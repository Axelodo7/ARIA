"""Google Drive footage stub.

Real Drive API integration (auth, listing, downloading) is a later job. For now
list_footage() returns a fixed catalogue of realistic-looking clips so Ava has
plausible material to reason over, and download_clip() writes a placeholder file
instead of fetching bytes.
"""
import os

# Fixed fake catalogue. Includes at least one ["bug-fix-demo"] clip (for
# security/bug content) and one ["user-praise"] clip (for testimonial content).
FOOTAGE = [
    {"id": "f_001", "filename": "screenrec_upload_fix.mp4", "duration_sec": 18,
     "tags": ["bug-fix-demo", "ui-demo"],
     "thumbnail_url": "https://drive.example/thumb/f_001.jpg"},
    {"id": "f_002", "filename": "founder_talking_head.mp4", "duration_sec": 25,
     "tags": ["founder", "talking-head"],
     "thumbnail_url": "https://drive.example/thumb/f_002.jpg"},
    {"id": "f_003", "filename": "happy_user_testimonial.mp4", "duration_sec": 32,
     "tags": ["user-praise", "user-testimonial"],
     "thumbnail_url": "https://drive.example/thumb/f_003.jpg"},
    {"id": "f_004", "filename": "dashboard_broll.mp4", "duration_sec": 12,
     "tags": ["b-roll", "ui-demo"],
     "thumbnail_url": "https://drive.example/thumb/f_004.jpg"},
    {"id": "f_005", "filename": "dark_mode_toggle_demo.mp4", "duration_sec": 9,
     "tags": ["ui-demo", "feature-demo"],
     "thumbnail_url": "https://drive.example/thumb/f_005.jpg"},
    {"id": "f_006", "filename": "security_lock_animation.mp4", "duration_sec": 6,
     "tags": ["b-roll", "security"],
     "thumbnail_url": "https://drive.example/thumb/f_006.jpg"},
    {"id": "f_007", "filename": "onboarding_flow_screencap.mp4", "duration_sec": 20,
     "tags": ["ui-demo", "onboarding"],
     "thumbnail_url": "https://drive.example/thumb/f_007.jpg"},
    {"id": "f_008", "filename": "team_working_broll.mp4", "duration_sec": 15,
     "tags": ["b-roll", "team"],
     "thumbnail_url": "https://drive.example/thumb/f_008.jpg"},
]


def list_footage():
    """Return the footage catalogue (a copy so callers can't mutate the stub)."""
    return [dict(f) for f in FOOTAGE]


def get_footage(footage_id):
    """Return a single footage entry by id, or None."""
    return next((dict(f) for f in FOOTAGE if f["id"] == footage_id), None)


def download_clip(footage_id, dest_path):
    """Write a placeholder file standing in for the downloaded clip. No real
    network/Drive access. Returns dest_path."""
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(f"PLACEHOLDER FOOTAGE for {footage_id} (stub — no real download)\n")
    return dest_path
