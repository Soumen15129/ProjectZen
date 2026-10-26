"""
test_route_e2e.py — drive the real POST /generate route end to end, with no LLM calls
and without touching the real database.

    python tests/test_route_e2e.py

The other tests exercise the pipeline. This one exercises app.py itself — input
resolution, Delta Update, the coverage report and every pre-existing error path —
through FastAPI's TestClient, with:

  * the capturing stub transport from test_prompt_identity.py (zero model calls)
  * PROJECTZEN_DATA_DIR pointed at a fresh temporary folder, so the database, the
    document store and the run logs it writes are all thrown away afterwards
"""
import os, sys, tempfile, shutil, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
MIDDLEWARE = os.path.dirname(HERE)
REAL_DB = os.path.join(os.path.dirname(MIDDLEWARE), "storage", "documents.db")

TMP = tempfile.mkdtemp(prefix="pz-route-test-")
os.environ["PROJECTZEN_DATA_DIR"] = TMP            # must precede any import of paths

_spec = importlib.util.spec_from_file_location(
    "identity", os.path.join(HERE, "test_prompt_identity.py"))
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)                       # installs the stub transport

from fastapi.testclient import TestClient
import app as appmod

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def payload(prompt, files=(), fmt="docx", **extra):
    body = {"userPrompt": prompt, "outputFormat": fmt, "username": "route-test",
            "fileDataList": [{"type": "text", "ext": "txt", "name": n, "content": t}
                             for n, t in files]}
    body.update(extra)
    return body


EXPECTED_KEYS = {"success", "fileId", "fileName", "outputFormat", "sizeKB", "downloadUrl",
                 "preview", "inputReport", "formatNote", "authoring", "version"}


def main():
    real_before = (os.path.getmtime(REAL_DB), os.path.getsize(REAL_DB)) \
        if os.path.exists(REAL_DB) else None
    PROMPT = "Template: Route Test Doc\n\nGenerate the document."

    with TestClient(appmod.app) as c:
        print("\nR1  single small file")
        small = T.text("SMALL", 20_000)
        r = c.post("/generate", json=payload(PROMPT, [("small.txt", small)]))
        check("200 OK", r.status_code == 200, r.text[:200])
        d = r.json() if r.status_code == 200 else {}
        check("response keys unchanged", EXPECTED_KEYS <= set(d), sorted(EXPECTED_KEYS - set(d)))
        warns = [w["file"] for w in (d.get("inputReport") or {}).get("warnings", [])]
        check("no coverage warning for a small input", "Input coverage" not in warns, warns)
        cov = (d.get("inputReport") or {}).get("coverage", {}).get("files", [])
        check("coverage: 1 file, fully read",
              len(cov) == 1 and cov[0]["generation"] == 100 and cov[0]["analysis"] == 100, cov)
        check("version is null on a New Generation", d.get("version") is None)
        base_id = d.get("fileId")

        def report(resp):
            return (resp.json() if resp.status_code == 200 else {}).get("inputReport") or {}

        def warning(rep, name):
            return next((w["message"] for w in rep.get("warnings", []) if w["file"] == name), "")

        print("\nR2  three files, 90K combined — Weekly Status size, now read in full")
        files = [(f"f{i}.txt", T.text(f"F{i}", 30_000)) for i in range(3)]
        r = c.post("/generate", json=payload(PROMPT, files))
        check("200 OK", r.status_code == 200, r.text[:200])
        rep = report(r)
        check("no coverage warning — everything read", not warning(rep, "Input coverage"))
        rows = rep.get("coverage", {}).get("files", [])
        check("all three files 100% read",
              len(rows) == 3 and all(x["generation"] == 100 and x["analysis"] == 100
                                     for x in rows), rows)

        print("\nR2b three files, 450K — over the window, under the limit: fair share")
        files = [(f"g{i}.txt", T.text(f"G{i}", 150_000)) for i in range(3)]
        T.CAPTURED.clear()
        r = c.post("/generate", json=payload(PROMPT, files))
        research = [p["prompt"] for p in T.CAPTURED if "parent document:" in p["prompt"]]
        check("the model actually sees ALL THREE files (research prompt)",
              bool(research) and all(f"G{i} line" in research[0] for i in range(3)))
        gen = [p["prompt"] for p in T.CAPTURED if p["kind"] == "generate"]
        check("...and so does generation", bool(gen) and all(f"G{i} line" in gen[0] for i in range(3)))
        check("cut files carry the continuation marker",
              bool(research) and "more characters of this file not shown" in research[0])
        check("accepted (200), as it was before Phase 2", r.status_code == 200, r.text[:200])
        rep = report(r)
        rows = rep.get("coverage", {}).get("files", [])
        check("every file contributes — none left at 0%",
              len(rows) == 3 and all(x["generation"] > 0 for x in rows), rows)
        check("equal files get equal shares (~66%)",
              all(60 <= x["generation"] <= 67 for x in rows), [x["generation"] for x in rows])
        msg = warning(rep, "Input coverage")
        check("warning says each file got a fair share", "fair share" in msg, msg[:160])

        print("\nR2c over the window because of a translation sheet: trimmed, then read in full")
        wb = ("[Config]\n" + "\n".join(f"field_{i},Label {i},Text" for i in range(3_000))
              + "\n\n[Translations]\n" + "\n".join(f"Label {i},Etikett {i},标签 {i}"
                                                  for i in range(3_000)))
        files = [("config.xlsx", wb), ("notes.txt", T.text("NOTE", 160_000))]
        body = payload(PROMPT, files)
        body["fileDataList"][0]["ext"] = "xlsx"
        r = c.post("/generate", json=body)
        check("200 OK", r.status_code == 200, r.text[:200])
        rep = report(r)
        msg = warning(rep, "Sheets left out")
        check("disclosed: Translations sheet left out", "Translations" in msg, msg[:160])
        rows = rep.get("coverage", {}).get("files", [])
        check("after trimming, both files read in full",
              len(rows) == 2 and all(x["generation"] == 100 for x in rows), rows)

        print("\nR3  Delta Update chain")
        r = c.post("/generate", json=payload(PROMPT, [], deltaSourceId=base_id,
                                             deltaChangeText="Week 12 update"))
        check("200 OK with no new files", r.status_code == 200, r.text[:200])
        v = (r.json() if r.status_code == 200 else {}).get("version") or {}
        check("saved as v2", v.get("version") == 2, v)
        r = c.post("/generate", json=payload(PROMPT, [("note.txt", "extra input")],
                                             deltaSourceId=r.json().get("fileId"),
                                             deltaChangeText="Week 13 update"))
        v = (r.json() if r.status_code == 200 else {}).get("version") or {}
        check("delta on v2 with a new file -> v3", v.get("version") == 3, v)
        names = [x["name"] for x in (r.json().get("inputReport") or {})
                 .get("coverage", {}).get("files", [])]
        check("coverage includes previous version AND the new file",
              len(names) == 2 and "note.txt" in names, names)

        print("\nR3b Delta Update with large new inputs: previous version kept, inputs shared")
        big = [(f"h{i}.txt", T.text(f"H{i}", 200_000)) for i in range(2)]
        T.CAPTURED.clear()
        r = c.post("/generate", json=payload(PROMPT, big, deltaSourceId=base_id,
                                             deltaChangeText="Week 14 update"))
        check("200 OK", r.status_code == 200, r.text[:200])
        rows = (report(r).get("coverage") or {}).get("files", [])
        check("small previous version kept whole",
              bool(rows) and rows[0]["generation"] == 100, rows[:1])
        check("both new inputs contribute", len(rows) == 3
              and all(x["generation"] > 0 for x in rows[1:]), rows)
        gen = [p["prompt"] for p in T.CAPTURED if p["kind"] == "generate"]
        check("generation still frames the previous version for updating",
              bool(gen) and "PREVIOUS VERSION OF THIS DOCUMENT" in gen[0]
              and "H0 line" in gen[0] and "H1 line" in gen[0])

        print("\nR4-R7  pre-existing behaviour")
        big = [(f"b{i}.txt", T.text(f"B{i}", 200_000)) for i in range(3)]
        r = c.post("/generate", json=payload(PROMPT, big))
        check("over 500K still refused with 413", r.status_code == 413, r.status_code)
        check("413 keeps its opening sentence", "too large to process together" in r.text)
        check("413 sizes now say 'characters', not 'KB'",
              "K characters" in r.text and " KB" not in r.text)
        check("413 advises splitting, not deleting first",
              "one module at a time" in r.text and "remove the largest" not in r.text)
        r = c.post("/generate", json=payload(PROMPT, []))
        check("prompt only, no files -> 200", r.status_code == 200, r.text[:200])
        r = c.post("/generate", json=payload(PROMPT, [], deltaSourceId="nope",
                                             deltaChangeText="x"))
        check("unknown delta source -> 404", r.status_code == 404, r.status_code)
        r = c.post("/generate", json=payload("Template: Nope\n\nx", [], fmt="pptx"))
        check("pptx with no reference -> 400 refusal", r.status_code == 400, r.status_code)
        r = c.post("/generate", json=payload("", []))
        check("empty prompt -> 400", r.status_code == 400, r.status_code)

    real_after = (os.path.getmtime(REAL_DB), os.path.getsize(REAL_DB)) \
        if os.path.exists(REAL_DB) else None
    print()
    check("real storage/documents.db untouched", real_before == real_after)
    print("\nRESULT:", "PASS" if not FAILS else f"FAIL ({len(FAILS)}): {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(rc)
