# llm-offload

Personal tooling lane for mechanical bulk work such as classification, extraction, rewriting, scoring and embeddings. It is not fleet infrastructure; applications own their own model choices.

## Use

```python
from aigw import chat
labels = chat("offload", "Classify this text", json_mode=True, project="<app>")
```

The executable entrypoints are [`client/aigw.py`](client/aigw.py) and [`tiers.json`](tiers.json). Provider order is explicit in the tier file. The gateway token is the only credential used by this client; no provider-specific credential is read from the environment.

| tier | for |
|---|---|
| `offload` | bulk / deterministic / dev work |
| `low` | classify, tag, extract, short generation |
| `high` | reasoning, structured extraction |

The gateway can fail over only through the providers listed for that tier. A failed tier raises an error; it never invents a result or silently changes capability.
