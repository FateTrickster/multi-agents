from app.config import settings

# Keep a single configured LLM for this experiment. No model fallbacks.
MODEL_CANDIDATES = [settings.AGENT_MODEL]
