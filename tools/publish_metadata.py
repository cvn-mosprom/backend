"""Sign the scanned digest and its build provenance; hand off all source coordinates."""
import json
import os
import subprocess
from pathlib import Path

name = os.environ["IMAGE"]
repository = f"ghcr.io/cvn-mosprom/{name}"
tag = f'{repository}:{os.environ.get("IMAGE_TAG_PREFIX", "sha")}-{os.environ["GITHUB_SHA"]}'
digest = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", tag, "--format", "{{.Manifest.Digest}}"], text=True).strip()
ref = repository + "@" + digest
provenance = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Provenance.SLSA}}"], text=True)
data = json.loads(provenance)
if not isinstance(data, dict) or not data:
    raise SystemExit("Build provenance missing")
data["mospromSource"] = {"backendSha": os.environ["GITHUB_SHA"], "frontendSha": os.environ["FRONTEND_SHA"]}
if os.environ.get("RELEASE_PROFILE"):
    data["mospromSource"]["profile"] = os.environ["RELEASE_PROFILE"]
    data["mospromSource"]["runId"] = os.environ["GITHUB_RUN_ID"]
    data["mospromSource"]["runAttempt"] = os.environ["GITHUB_RUN_ATTEMPT"]
Path("provenance.json").write_text(json.dumps(data))
subprocess.run(["cosign", "sign", "--yes", ref], check=True)
subprocess.run(["cosign", "attest", "--yes", "--type", "slsaprovenance", "--predicate", "provenance.json", ref], check=True)
result = {"name": name, "repository": repository, "digest": digest,
                                         "sourceSha": os.environ["GITHUB_SHA"], "frontendSha": os.environ["FRONTEND_SHA"],
                                         "profile": os.environ.get("RELEASE_PROFILE", "distributed-v1")}
if os.environ.get("RELEASE_PROFILE"):
    result.update(runId=os.environ["GITHUB_RUN_ID"], runAttempt=os.environ["GITHUB_RUN_ATTEMPT"])
Path("image.json").write_text(json.dumps(result) + "\n")
