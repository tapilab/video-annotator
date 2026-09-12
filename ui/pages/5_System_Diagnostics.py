"""
system_diagnostics.py - System Diagnostics page for VANTAGE-AI
"""

import sys
sys.path.append("..")
import os
import streamlit as st
from utils import SEARCH_ADMIN_KEY, AZURE_STORAGE_KEY, SEARCH_FN_URL

STAGE_MEDIA_URL = os.environ.get("STAGE_MEDIA_URL", "")
TRANSCRIBE_URL = os.environ.get("TRANSCRIBE_URL", "")
EMBED_INDEX_URL = os.environ.get("EMBED_INDEX_URL", "")

APP_TITLE = "VANTAGE-AI: Video ANnotation, TAGging & Exploration"
st.title(APP_TITLE, anchor=False)
st.subheader("⚙️ System Diagnostics")
st.info("Check system configuration and troubleshoot issues")

# Configuration status
st.subheader("Configuration Status")

config_checks = {
    "Azure Search (SEARCH_ADMIN_KEY)": bool(SEARCH_ADMIN_KEY),
    "Azure Storage (AZURE_STORAGE_KEY)": bool(AZURE_STORAGE_KEY),
    "Search Function (SEARCH_FN_URL)": bool(SEARCH_FN_URL),
    "Stage Media Function (STAGE_MEDIA_URL)": bool(STAGE_MEDIA_URL),
    "Transcribe Function (TRANSCRIBE_URL)": bool(TRANSCRIBE_URL),
    "Embed & Index Function (EMBED_INDEX_URL)": bool(EMBED_INDEX_URL),
}

cols = st.columns(2)
for i, (name, status) in enumerate(config_checks.items()):
    icon = "✅" if status else "❌"
    cols[i % 2].write(f"{icon} {name}: {'OK' if status else 'Not configured'}")

# Debug info
st.markdown("---")
st.subheader("Debug Information")

with st.expander("Session State"):
    st.json({
        k: str(v)[:100] + "..." if len(str(v)) > 100 else v
        for k, v in st.session_state.items()
    })

with st.expander("Recent Processing Debug"):
    if st.session_state.get('debug_info'):
        st.json(st.session_state['debug_info'])
    else:
        st.info("No debug info yet. Process a video first.")