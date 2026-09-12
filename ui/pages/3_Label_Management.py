"""
1_Label_Management.py - Streamlit Page for Label Management

Provides UI for managing label definitions:
- View all existing labels
- Add new labels with name and description
- Edit existing labels
- Deactivate labels

Calls ManageLabels Azure Function for all operations.

Configuration (via .env):
  - MANAGE_LABELS_URL: ManageLabels function endpoint
"""

import sys
sys.path.append("..")
import os
import requests
import streamlit as st
from dotenv import load_dotenv
from utils import format_timestamp

load_dotenv()

MANAGE_LABELS_URL = os.environ.get("MANAGE_LABELS_URL", "")

APP_TITLE = "VANTAGE-AI: Video ANnotation, TAGging & Exploration"
st.title(APP_TITLE, anchor=False)
st.subheader("Label Library Management")

if not MANAGE_LABELS_URL:
    st.error("MANAGE_LABELS_URL must be configured in .env")
    st.stop()


def call_labels_api(method: str = "GET", payload: dict = None) -> dict:
    """Call ManageLabels Azure Function."""
    method = method.upper()
    try:
        r = requests.request(
            method,
            MANAGE_LABELS_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        if r.status_code >= 400:
            error_msg = r.json().get("error", r.text) if r.text else f"HTTP {r.status_code}"
            st.error(f"API Error: {error_msg}")
            return {}
        data = r.json() if r.text else {}
        if method != "GET":
            get_label_library.clear()
        return data
    except requests.exceptions.RequestException as e:
        print(f"call_labels_api failed: {e}")
        st.error("Connection error — the label service didn't respond. Please try again in a moment.")
        return {}


@st.cache_data(ttl=8, show_spinner=False)
def get_label_library() -> dict:
    """Short-lived cache to avoid repeated GET requests per rerun."""
    return call_labels_api("GET")


# --- Labeling status banner ---
library = get_label_library()
labels = library.get("labels", []) if isinstance(library, dict) else []
_pending = any(not l.get("applied", True) for l in labels)
if _pending:
    _status = library.get("labeling_status")
    if _status and _status.get("status") == "running":
        _completed = _status.get("completed", 0)
        _total = _status.get("total", 0)
        st.warning(f"Labeling in progress — {_completed}/{_total} videos labeled. Refresh to check status.")
    else:
        st.warning("Labeling in progress — search results will update once complete. Refresh to check status.")

# --- Re-run all labels ---
with st.expander("Re-run all labels"):
    st.caption("Resets all labels and re-applies them to every segment.")

    if not st.session_state.get('confirm_rerun_labels'):
        if st.button("Re-run all labels", type="primary"):
            st.session_state['confirm_rerun_labels'] = True
            st.rerun()
    else:
        st.warning("This resets every label and re-runs AI labeling across every segment in every video. Are you sure you want to continue?")
        col_confirm, col_cancel = st.columns(2)
        with col_confirm:
            if st.button("Yes, re-run all labels", type="primary", use_container_width=True):
                st.session_state['confirm_rerun_labels'] = False
                result = call_labels_api("PATCH")
                if result:
                    st.success(result.get("message", "Re-labeling queued."))
                st.rerun()
        with col_cancel:
            if st.button("Cancel", use_container_width=True):
                st.session_state['confirm_rerun_labels'] = False

# --- Tab layout ---
tab_view, tab_add, tab_edit = st.tabs(["View Labels", "Add Label", "Edit Label"])

# --- TAB 1: View Labels ---
with tab_view:
    if st.button("Refresh"):
        get_label_library.clear()
        st.rerun()

    if library and "labels" in library:
        st.caption(f"**Last Updated:** {format_timestamp(library.get('last_updated', 'N/A'))}")

        if not library["labels"]:
            st.info("No labels defined yet. Add labels in the 'Add Label' tab.")
        else:
            st.write(f"**Total Labels:** {len(library['labels'])}")

            for i, label in enumerate(library["labels"], 1):
                with st.expander(f"{i}. {label['name']}", expanded=False):
                    st.write(f"**Description:** {label['description']}")
                    st.write(f"**Label ID:** `{label['label_id']}`")
                    st.write(f"**Created:** {format_timestamp(label['created_at'])}")
                    st.write(f"**Last Updated:** {format_timestamp(label['updated_at'])}")
                    examples = label.get("examples", [])
                    if examples:
                        st.write(f"**Positive Examples ({len(examples)}/3):**")
                        for ex in examples:
                            st.caption(f"• _{ex}_")

# --- TAB 2: Add Label ---
with tab_add:
    with st.form("add_label_form", clear_on_submit=True):
        new_name = st.text_input("Label Name", placeholder="e.g., Misinformation")
        new_desc = st.text_area(
            "Description",
            placeholder="Describe what this label represents...",
            help="This description will be used by AI to understand when to apply this label.",
        )
        st.caption("Positive Examples (optional, up to 3) — paste example text that represents this label:")
        ex1 = st.text_area("Example 1", placeholder="e.g., a transcript segment that clearly shows this label...", height=80)
        ex2 = st.text_area("Example 2", placeholder="", height=80)
        ex3 = st.text_area("Example 3", placeholder="", height=80)
        add_submit = st.form_submit_button("Add Label", type="primary")

        if add_submit:
            if not new_name or not new_desc:
                st.error("Both name and description are required.")
            elif any(len(e.split()) > 100 for e in [ex1, ex2, ex3] if e.strip()):
                st.error("Examples must be 100 words or fewer.")
            else:
                examples = [e.strip() for e in [ex1, ex2, ex3] if e.strip()]
                result = call_labels_api("POST", {"name": new_name, "description": new_desc, "examples": examples})
                if result and "label_id" in result:
                    st.success(f"Label '{result['name']}' added!")
                    st.info("Labeling queued — updated labels will appear in search results shortly.")
                    st.rerun()

# --- TAB 3: Edit Label ---
with tab_edit:
    if library and library.get("labels"):
        label_options = {l["name"]: l for l in library["labels"]}
        selected_name = st.selectbox("Select Label", options=list(label_options.keys()))

        if selected_name:
            label = label_options[selected_name]

            with st.form("edit_label_form"):
                edit_name = st.text_input("Label Name", value=label["name"])
                edit_desc = st.text_area("Description", value=label["description"])

                existing_examples = label.get("examples", [])
                st.caption("Positive Examples (up to 3) — edit or clear to remove:")
                ex_vals = existing_examples + [""] * (3 - len(existing_examples))
                ex1 = st.text_area("Example 1", value=ex_vals[0], height=80)
                ex2 = st.text_area("Example 2", value=ex_vals[1], height=80)
                ex3 = st.text_area("Example 3", value=ex_vals[2], height=80)

                col_update, col_delete = st.columns(2)
                with col_update:
                    update_submit = st.form_submit_button("Update", type="primary", use_container_width=True)
                with col_delete:
                    delete_submit = st.form_submit_button("Deactivate", use_container_width=True)

                if update_submit:
                    if any(len(e.split()) > 100 for e in [ex1, ex2, ex3] if e.strip()):
                        st.error("Examples must be 100 words or fewer.")
                    else:
                        examples = [e.strip() for e in [ex1, ex2, ex3] if e.strip()]
                        result = call_labels_api("PUT", {
                            "label_id": label["label_id"],
                            "name": edit_name,
                            "description": edit_desc,
                            "examples": examples,
                        })
                        if result and "label_id" in result:
                            st.success("Label updated!")
                            st.info("Labeling queued — updated labels will appear in search results shortly.")
                            st.rerun()

            if delete_submit:
                st.session_state['confirm_deactivate_label'] = label["label_id"]
                st.rerun()

            if st.session_state.get('confirm_deactivate_label') == label["label_id"]:
                st.warning(f"Deactivate **{label['name']}**? This label will stop being applied to new segments.")
                col_yes, col_no = st.columns(2)
                with col_yes:
                    if st.button("Yes, deactivate", type="primary", use_container_width=True, key=f"confirm_deact_{label['label_id']}"):
                        st.session_state['confirm_deactivate_label'] = None
                        result = call_labels_api("DELETE", {"label_id": label["label_id"]})
                        if result and result.get("success"):
                            st.success("Label deactivated!")
                            st.info("Labeling queued — updated labels will appear in search results shortly.")
                            st.rerun()
                with col_no:
                    if st.button("Cancel", use_container_width=True, key=f"cancel_deact_{label['label_id']}"):
                        st.session_state['confirm_deactivate_label'] = None
                        st.rerun()
    else:
        st.info("No labels available to edit. Add a label first.")
