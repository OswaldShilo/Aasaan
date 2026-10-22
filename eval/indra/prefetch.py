"""Docker build step: copy the Indra models into INDRA_OCR_MODEL_DIR so the container never downloads anything at run time."""
import os

from huggingface_hub import snapshot_download
from indra_ocr.models import HF_REPO, HF_REVISION

dest = os.environ["INDRA_OCR_MODEL_DIR"]
snapshot_download(HF_REPO, revision=HF_REVISION, local_dir=dest, allow_patterns=["*.onnx", "meta.json", "config.json"])
print("models in", dest, sorted(os.listdir(dest)))
