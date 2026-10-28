import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("APP_ENV", "demo")
os.environ.pop("ANTHROPIC_API_KEY", None)  # tests never call a real LLM
