"""Florence-2-base-ft text reader (local CPU inference, no image leaves the machine). Loaded once, on first use or at API startup.
Weights: microsoft/Florence-2-base-ft as converted to transformers' native layout (config.FLORENCE_MODEL) -- Microsoft's own repo ships
remote code that does not run on transformers 5.x. English text only; it gives no per-word confidence, so callers must not present it as certain.
Its per-token probabilities are the only confidence signal: real text scores ~0.76, garbled text ~0.6, and the text it hallucinates on blank/noise
images ~0.5 (measured on generated fixtures), so output below MIN_CONF is refused. Every failure or refusal returns None so the caller falls back to Tesseract."""
import threading
from typing import Optional

from PIL import Image

import config, log

TASK = "<OCR_WITH_REGION>"       # one text string per detected line, in reading order
MIN_CONF = 0.65                  # mean token probability below this = treated as unreadable, never shown
MAX_NEW_TOKENS = 1024            # the model's limit; each line costs ~8 location tokens plus its text

_load_lock, _infer_lock = threading.Lock(), threading.Lock()     # inference is CPU-bound and multi-threaded inside torch: one photo at a time
_model = _proc = None
_failed = False


def load() -> bool:
    """True once the model is ready. Thread-safe; a failed load (missing packages, offline first run) is remembered until restart."""
    global _model, _proc, _failed
    if not config.FLORENCE:
        return False
    if _model is not None:
        return True
    if _failed:
        return False
    with _load_lock:
        if _model is not None:
            return True
        try:
            import torch
            from transformers import AutoProcessor, Florence2ForConditionalGeneration
            _proc = AutoProcessor.from_pretrained(config.FLORENCE_MODEL)
            _model = Florence2ForConditionalGeneration.from_pretrained(config.FLORENCE_MODEL, dtype=torch.float32).eval()
            log.event("florence_loaded", model=config.FLORENCE_MODEL)
        except Exception as e:
            _failed = True
            log.event("florence_unavailable", error=type(e).__name__, detail=str(e)[:200], level="warn")
            return False
    return True


def read_lines(rgb: Image.Image) -> Optional[tuple]:
    """(lines in reading order, confidence 0-100) or None = Florence-2 off/unavailable/failed/not confident (caller falls back to Tesseract)."""
    if not load():
        return None
    try:
        import torch
        with _infer_lock, torch.inference_mode():
            inp = _proc(text=TASK, images=rgb, return_tensors="pt")
            gen = _model.generate(input_ids=inp["input_ids"], pixel_values=inp["pixel_values"], max_new_tokens=MAX_NEW_TOKENS, num_beams=1,
                                  do_sample=False, output_scores=True, return_dict_in_generate=True)
            conf = float(_model.compute_transition_scores(gen.sequences, gen.scores, normalize_logits=True)[0].exp().mean())
            out = _proc.post_process_generation(_proc.batch_decode(gen.sequences, skip_special_tokens=False)[0], task=TASK, image_size=rgb.size)
        if conf < MIN_CONF:
            return None
        return [l.strip() for l in out[TASK]["labels"] if l.strip()], round(conf * 100, 1)
    except Exception as e:
        log.event("florence_failed", error=type(e).__name__, detail=str(e)[:200], level="warn")
        return None
