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
parameters = data.get("buildDefinition", {}).get("externalParameters")
if not isinstance(parameters, dict):
    raise SystemExit("Build provenance must contain SLSA v1 external parameters")
source = {"backendSha": os.environ["GITHUB_SHA"], "frontendSha": os.environ["FRONTEND_SHA"]}
if os.environ.get("RELEASE_PROFILE"):
    source["profile"] = os.environ["RELEASE_PROFILE"]
    source["runId"] = os.environ["GITHUB_RUN_ID"]
    source["runAttempt"] = os.environ["GITHUB_RUN_ATTEMPT"]
# SLSA's parameters are extensible. A top-level custom field is discarded by cosign's typed serializer.
parameters["mospromSource"] = source
Path("provenance.json").write_text(json.dumps(data))
subprocess.run(["cosign", "sign", "--yes", ref], check=True)
subprocess.run(["cosign", "attest", "--yes", "--type", "slsaprovenance1", "--predicate", "provenance.json", ref], check=True)
result = {"name": name, "repository": repository, "digest": digest,
                                         "sourceSha": os.environ["GITHUB_SHA"], "frontendSha": os.environ["FRONTEND_SHA"],
                                         "profile": os.environ.get("RELEASE_PROFILE", "distributed-v1")}
if os.environ.get("RELEASE_PROFILE"):
    result.update(runId=os.environ["GITHUB_RUN_ID"], runAttempt=os.environ["GITHUB_RUN_ATTEMPT"])
Path("image.json").write_text(json.dumps(result) + "\n")
