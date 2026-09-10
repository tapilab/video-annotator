# Video Annotator UI (Streamlit)

A minimal Streamlit UI for searching indexed video segments via the `SearchSegments` Azure Function.

## Prereqs
- Python 3.11+
- A working `SearchSegments` Function URL (includes `?code=...`)

---

## Local run (development)

### 1) Create venv + install deps
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
````

### 2) Configure environment

Create `ui/.env` (do not commit):

```env
SEARCH_FN_URL=https://<your-function-app>.azurewebsites.net/api/SearchSegments?code=...
MANAGE_LABELS_URL=https://<your-function-app>.azurewebsites.net/api/ManageLabels?code=...
EVAL_LABELS_URL=https://<your-function-app>.azurewebsites.net/api/EvalLabels?code=...
STAGE_MEDIA_URL=https://<your-function-app>.azurewebsites.net/api/StageMedia?code=...
TRANSCRIBE_URL=https://<your-function-app>.azurewebsites.net/api/TranscribeHttp?code=...
EMBED_INDEX_URL=https://<your-function-app>.azurewebsites.net/api/EmbedAndIndex?code=...
SEARCH_ENDPOINT=https://<your-search-service>.search.windows.net
SEARCH_ADMIN_KEY=<your-search-admin-key>
AZURE_STORAGE_ACCOUNT=<your-storage-account>
AZURE_STORAGE_KEY=<your-storage-key>
```

### 3) Run Streamlit (IMPORTANT: use python -m)

```bash
python -m streamlit run ui_search.py
```

Open the URL Streamlit prints (usually [http://localhost:8501](http://localhost:8501)).

---

## Development workflow

* Edit `ui_search.py` and Streamlit will auto-reload.
* If you change dependencies, update `requirements.txt` and re-run:

  ```bash
  pip install -r requirements.txt
  ```

### Known dependency note (Plotly)

Streamlit imports Plotly theming even if you don’t use charts, so `requirements.txt` pins Plotly to a compatible version (`plotly==5.24.1`).

---

## Deploy to Azure Container Apps

### One-time: create/update the container app

From the `ui/` directory:

```bash
RG="video-annotator-robot"
LOC="eastus"
APP="video-annotator-ui"

az containerapp up \
  --name "$APP" \
  --resource-group "$RG" \
  --location "$LOC" \
  --source .
```

### Set env vars (server-side; keeps function key out of the browser)

```bash
az containerapp update \
  -g "$RG" -n "$APP" \
  --set-env-vars \
    SEARCH_FN_URL="https://<your-function-app>.azurewebsites.net/api/SearchSegments?code=..." \
    MANAGE_LABELS_URL="https://<your-function-app>.azurewebsites.net/api/ManageLabels?code=..." \
    EVAL_LABELS_URL="https://<your-function-app>.azurewebsites.net/api/EvalLabels?code=..." \
    STAGE_MEDIA_URL="https://<your-function-app>.azurewebsites.net/api/StageMedia?code=..." \
    TRANSCRIBE_URL="https://<your-function-app>.azurewebsites.net/api/TranscribeHttp?code=..." \
    EMBED_INDEX_URL="https://<your-function-app>.azurewebsites.net/api/EmbedAndIndex?code=..." \
    SEARCH_ENDPOINT="https://<your-search-service>.search.windows.net" \
    SEARCH_ADMIN_KEY="<your-search-admin-key>" \
    AZURE_STORAGE_ACCOUNT="<your-storage-account>" \
    AZURE_STORAGE_KEY="<your-storage-key>"
```

### Scale to zero (optional)

```bash
az containerapp update -g "$RG" -n "$APP" --min-replicas 0 --max-replicas 1
```

### Get the public URL

```bash
az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv
```

### View logs

```bash
az containerapp logs show -g "$RG" -n "$APP" --follow
```

---

## Troubleshooting

### `streamlit run ...` uses the wrong Python

If you see imports from `/usr/local/...` instead of your venv, always run:

```bash
python -m streamlit run ui_search.py
```


