"""
2_Label_Evaluation.py - Streamlit Page for Label Accuracy Evaluation

Upload a CSV with 'text' and 'labels' columns to evaluate GPT labeling
accuracy against manually annotated ground truth.

Configuration (via .env):
  - EVAL_LABELS_URL: EvalLabels function endpoint
"""

import io
import json
import os

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

EVAL_LABELS_URL = os.environ.get("EVAL_LABELS_URL", "")

APP_TITLE = "VANTAGE-AI: Video ANnotation, TAGging & Exploration"
st.title(APP_TITLE, anchor=False)
st.subheader("Label Accuracy Evaluation")

if not EVAL_LABELS_URL:
    st.error("EVAL_LABELS_URL must be configured in .env")
    st.stop()

st.markdown(
    "Upload a CSV with two columns: **`text`** (transcript text) and **`labels`** "
    "(comma-separated expected labels). The evaluator will run the active label library "
    "against each row and compare GPT predictions to your annotations."
)

uploaded = st.file_uploader("Upload CSV", type=["csv"])

if not uploaded:
    st.stop()

try:
    df = pd.read_csv(uploaded)
except Exception as e:
    st.error(f"Could not parse CSV: {e}")
    st.stop()

if "text" not in df.columns or "labels" not in df.columns:
    st.error("CSV must have 'text' and 'labels' columns.")
    st.stop()

df = df.dropna(subset=["text"])
df["labels"] = df["labels"].fillna("").astype(str)

st.subheader("Preview")
st.dataframe(df.head(10), width="stretch")
st.caption(f"{len(df)} rows loaded.")

if not st.button("Run Evaluation", type="primary"):
    st.stop()

# Build test cases
test_cases = []
for _, row in df.iterrows():
    raw_labels = [l.strip() for l in str(row["labels"]).split(",") if l.strip()]
    test_cases.append({"text": str(row["text"]), "expected_labels": raw_labels})

with st.spinner(f"Running GPT on {len(test_cases)} rows..."):
    try:
        r = requests.post(
            EVAL_LABELS_URL,
            json={"test_cases": test_cases},
            headers={"Content-Type": "application/json"},
            timeout=300,
        )
        if r.status_code >= 400:
            st.error(f"API Error: {r.json().get('error', r.text)}")
            st.stop()
        result = r.json()
    except requests.exceptions.RequestException as e:
        print(f"Label evaluation request failed: {e}")
        st.error("Connection error — the evaluation service didn't respond. Please try again in a moment.")
        st.stop()

rows = result.get("rows", [])
metrics = result.get("metrics", {})
unknown_labels = result.get("unknown_labels", [])

if unknown_labels:
    st.warning(
        "The following labels from your CSV were **not found in the label library** and were ignored during evaluation:\n\n"
        + ", ".join(f"`{l}`" for l in unknown_labels)
    )

# --- Overall metrics ---
st.subheader("Overall Metrics")
col1, col2, col3, col4 = st.columns(4)
col1.metric(
    "Macro F1", f"{metrics.get('macro_f1', 0):.3f}",
    help="The average accuracy across all labels, treating each label equally regardless of how often it appears. Low macro F1 with high micro F1 usually means a rare label is being missed.",
)
col2.metric(
    "Micro F1", f"{metrics.get('micro_f1', 0):.3f}",
    help="Overall accuracy across every prediction combined — a single balance of precision and recall for the whole dataset.",
)
col3.metric(
    "Micro Precision", f"{metrics.get('micro_precision', 0):.3f}",
    help="Of all the labels the AI applied, what fraction were actually correct. Low precision means it's over-applying labels.",
)
col4.metric(
    "Micro Recall", f"{metrics.get('micro_recall', 0):.3f}",
    help="Of all the labels that should have been applied, what fraction the AI actually caught. Low recall means it's missing labels it should have found.",
)

# --- Per-label metrics ---
st.subheader("Per-Label Metrics")
per_label = metrics.get("per_label", {})
if per_label:
    label_df = pd.DataFrame([
        {
            "Label": label,
            "Precision": m["precision"],
            "Recall": m["recall"],
            "F1": m["f1"],
            "TP": m["tp"],
            "FP": m["fp"],
            "FN": m["fn"],
        }
        for label, m in per_label.items()
    ]).sort_values("F1", ascending=False)
    st.dataframe(label_df, width="stretch", hide_index=True)

# --- Per-row results ---
st.subheader("Per-Row Results")
rows_df = pd.DataFrame([
    {
        "Text": row["text"][:120] + "..." if len(row["text"]) > 120 else row["text"],
        "Expected": ", ".join(row["expected"]),
        "Predicted": ", ".join(row["predicted"]),
        "Correct": ", ".join(row["correct"]),
        "Missed": ", ".join(row["missed"]),
        "Hallucinated": ", ".join(row["hallucinated"]),
    }
    for row in rows
])
st.dataframe(rows_df, width="stretch", hide_index=True)

# --- Export ---
st.subheader("Export Results")

full_rows_df = pd.DataFrame([
    {
        "text": row["text"],
        "expected": ", ".join(row["expected"]),
        "predicted": ", ".join(row["predicted"]),
        "correct": ", ".join(row["correct"]),
        "missed": ", ".join(row["missed"]),
        "hallucinated": ", ".join(row["hallucinated"]),
    }
    for row in rows
])

col_csv, col_json = st.columns(2)

with col_csv:
    st.download_button(
        "Download rows as CSV",
        data=full_rows_df.to_csv(index=False),
        file_name="eval_results.csv",
        mime="text/csv",
        width="stretch",
    )

with col_json:
    st.download_button(
        "Download full results as JSON",
        data=json.dumps(result, indent=2, ensure_ascii=False),
        file_name="eval_results.json",
        mime="application/json",
        width="stretch",
    )
