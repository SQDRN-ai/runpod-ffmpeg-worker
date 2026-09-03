import sys


if len(sys.argv) != 2 or sys.argv[1] not in {"api", "worker"}:
    raise SystemExit("Usage: python -m app.entrypoint [api|worker]")

if sys.argv[1] == "api":
    import uvicorn

    uvicorn.run("app.api:app", host="0.0.0.0", port=8080)
else:
    from app.worker import run

    run()

