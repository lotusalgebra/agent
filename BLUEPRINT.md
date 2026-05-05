# LOTUS Agent — Full Automation Blueprint
# @techengine.lab Content Pipeline
# Mac Studio M1 Max 32GB | Gemma 4 26B-A4B | Claude.ai Pro

## Architecture Overview

```
VOICE COMMAND
     │
     ▼
┌────────────────────────────────────────────────────────────┐
│  GEMMA 4 26B-A4B (Local Brain via Ollama)                  │
│  Understands: "Make defense reel for Agni-6"               │
│  Routes to correct pipeline + fills templates               │
└────────┬───────────┬───────────┬───────────┬───────────────┘
         │           │           │           │
    ┌────▼───┐  ┌───▼────┐  ┌──▼───┐  ┌───▼────────┐
    │Pipeline│  │Pipeline│  │Route │  │Pipeline    │
    │  #1    │  │  #2    │  │  #3  │  │  #4        │
    │NB2 Gen │  │Grok    │  │Claude│  │Google Drive│
    │+ Save  │  │Video   │  │.ai   │  │Upload      │
    └────────┘  └────────┘  └──────┘  └────────────┘
```

---

## Pipeline #1: NB2 Image Generation + Auto-Download

### What it automates:
- Opens NB2 in browser
- Enters image prompt from template
- Waits for generation
- Downloads all frames
- Renames and organizes into project folder

### Code: `pipelines/nb2_generator.py`

```python
"""
NB2 Image Generator — Browser automation for image generation.
Adapts to whatever NB2 tool you use (web-based).
"""

import os
import time
import glob
import shutil
from datetime import datetime
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ═══════════════════════════════════════════════════════
#  CONFIG — Update these for your NB2 tool
# ═══════════════════════════════════════════════════════

NB2_URL = "https://your-nb2-tool.com"        # Your NB2 URL
NB2_PROMPT_SELECTOR = "textarea.prompt"       # CSS selector for prompt input
NB2_GENERATE_BUTTON = "button.generate"       # CSS selector for generate button
NB2_DOWNLOAD_BUTTON = "button.download"       # CSS selector for download
NB2_IMAGE_SELECTOR = "img.result"             # CSS selector for generated image

DOWNLOAD_DIR = os.path.expanduser("~/Downloads")
PROJECTS_DIR = os.path.expanduser("~/LotusAgent/Projects")
CHROME_PROFILE = os.path.expanduser("~/Library/Application Support/Google/Chrome/Default")


class NB2Generator:
    def __init__(self):
        self.driver = None
        self.project_dir = None

    def start_browser(self):
        """Launch Chrome with your existing profile (keeps logins)."""
        options = webdriver.ChromeOptions()
        options.add_argument(f"--user-data-dir={CHROME_PROFILE}")
        options.add_argument("--no-first-run")
        options.add_experimental_option("prefs", {
            "download.default_directory": DOWNLOAD_DIR,
            "download.prompt_for_download": False,
        })
        self.driver = webdriver.Chrome(options=options)
        self.driver.implicitly_wait(10)

    def create_project(self, name: str, pillar: str) -> str:
        """
        Create organized project folder.

        Structure:
          ~/LotusAgent/Projects/
            └── 2026-04-16_Defense_Agni6/
                ├── frames/          ← NB2 images
                ├── videos/          ← Grok clips
                ├── output/          ← Final reel
                ├── drive_uploaded/   ← Upload confirmations
                └── metadata.json    ← Project info
        """
        date = datetime.now().strftime("%Y-%m-%d")
        folder_name = f"{date}_{pillar}_{name}"
        self.project_dir = os.path.join(PROJECTS_DIR, folder_name)

        for sub in ["frames", "videos", "output", "drive_uploaded"]:
            os.makedirs(os.path.join(self.project_dir, sub), exist_ok=True)

        # Save metadata
        import json
        metadata = {
            "name": name,
            "pillar": pillar,
            "created": datetime.now().isoformat(),
            "status": "nb2_pending",
            "frames_generated": 0,
            "videos_generated": 0,
            "reel_exported": False,
            "drive_uploaded": False,
            "claude_score": None,
        }
        with open(os.path.join(self.project_dir, "metadata.json"), "w") as f:
            json.dump(metadata, f, indent=2)

        print(f"  📁 Project created: {self.project_dir}")
        return self.project_dir

    def generate_frames(self, prompts: list[str]) -> list[str]:
        """
        Generate images from NB2 for each prompt.

        Args:
            prompts: List of NB2 prompts (one per frame)

        Returns:
            List of saved file paths
        """
        if not self.driver:
            self.start_browser()

        saved_files = []
        frames_dir = os.path.join(self.project_dir, "frames")

        for i, prompt in enumerate(prompts, 1):
            print(f"\n  🎨 Generating Frame {i}/{len(prompts)}...")

            # Navigate to NB2
            self.driver.get(NB2_URL)
            time.sleep(2)

            # Enter prompt
            prompt_box = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, NB2_PROMPT_SELECTOR))
            )
            prompt_box.clear()
            prompt_box.send_keys(prompt)
            time.sleep(0.5)

            # Click generate
            gen_btn = self.driver.find_element(By.CSS_SELECTOR, NB2_GENERATE_BUTTON)
            gen_btn.click()

            # Wait for generation (NB2 typically takes 30-120 seconds)
            print(f"  ⏳ Waiting for generation...")
            WebDriverWait(self.driver, 180).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, NB2_IMAGE_SELECTOR))
            )
            time.sleep(2)  # Extra buffer for full render

            # Download
            dl_btn = self.driver.find_element(By.CSS_SELECTOR, NB2_DOWNLOAD_BUTTON)
            dl_btn.click()
            time.sleep(3)

            # Find newest download and move to project
            downloaded = self._get_latest_download()
            if downloaded:
                dest = os.path.join(frames_dir, f"Frame{i}.png")
                shutil.move(downloaded, dest)
                saved_files.append(dest)
                print(f"  ✅ Frame {i} saved: {dest}")
            else:
                print(f"  ❌ Frame {i} download failed")

        # Update metadata
        self._update_metadata({"frames_generated": len(saved_files), "status": "nb2_done"})
        return saved_files

    def _get_latest_download(self, timeout=30) -> str | None:
        """Wait for and return the most recently downloaded file."""
        start = time.time()
        while time.time() - start < timeout:
            files = glob.glob(os.path.join(DOWNLOAD_DIR, "*.png")) + \
                    glob.glob(os.path.join(DOWNLOAD_DIR, "*.jpg")) + \
                    glob.glob(os.path.join(DOWNLOAD_DIR, "*.webp"))
            if files:
                newest = max(files, key=os.path.getmtime)
                if time.time() - os.path.getmtime(newest) < 10:
                    return newest
            time.sleep(1)
        return None

    def _update_metadata(self, updates: dict):
        import json
        meta_path = os.path.join(self.project_dir, "metadata.json")
        with open(meta_path, "r") as f:
            meta = json.load(f)
        meta.update(updates)
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

    def close(self):
        if self.driver:
            self.driver.quit()
```

---

## Pipeline #2: Grok Video Generation

### What it automates:
- Opens Grok in browser
- Uploads each NB2 frame
- Enters animation prompt
- Downloads 6-second video clips
- Saves to project/videos/

### Code: `pipelines/grok_animator.py`

```python
"""
Grok Video Animator — Uploads NB2 frames and generates 6s clips.
"""

import os
import time
import glob
import shutil
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


GROK_URL = "https://x.com/i/grok"   # Grok image-to-video
DOWNLOAD_DIR = os.path.expanduser("~/Downloads")


class GrokAnimator:
    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        self.driver = None

    def start_browser(self):
        options = webdriver.ChromeOptions()
        profile = os.path.expanduser(
            "~/Library/Application Support/Google/Chrome/Default"
        )
        options.add_argument(f"--user-data-dir={profile}")
        options.add_experimental_option("prefs", {
            "download.default_directory": DOWNLOAD_DIR,
        })
        self.driver = webdriver.Chrome(options=options)
        self.driver.implicitly_wait(10)

    def animate_frames(self, frame_paths: list[str],
                       animation_prompts: list[str]) -> list[str]:
        """
        Upload each frame to Grok and generate video.

        Args:
            frame_paths: Paths to NB2 PNG frames
            animation_prompts: Matching Grok prompts (under 30 words each)

        Returns:
            List of video file paths
        """
        if not self.driver:
            self.start_browser()

        videos_dir = os.path.join(self.project_dir, "videos")
        saved_videos = []

        for i, (frame, prompt) in enumerate(zip(frame_paths, animation_prompts), 1):
            print(f"\n  🎬 Animating Frame {i}/{len(frame_paths)}...")

            self.driver.get(GROK_URL)
            time.sleep(3)

            # Upload image
            # Grok's file input — adapt selector to current UI
            file_input = self.driver.find_element(By.CSS_SELECTOR, "input[type='file']")
            file_input.send_keys(os.path.abspath(frame))
            time.sleep(2)

            # Enter animation prompt
            prompt_box = self.driver.find_element(By.CSS_SELECTOR, "textarea")
            prompt_box.clear()
            prompt_box.send_keys(prompt)
            time.sleep(0.5)

            # Submit
            prompt_box.send_keys("\n")  # Or find submit button

            # Wait for video generation (Grok takes 60-180 seconds)
            print(f"  ⏳ Generating video (this takes 1-3 minutes)...")
            time.sleep(120)  # Minimum wait

            # Check for video element and download
            # This part needs adaptation to Grok's current download flow
            try:
                video_el = WebDriverWait(self.driver, 120).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "video"))
                )

                # Try to find download option
                # Grok UI changes — may need right-click save or explicit button
                time.sleep(5)

                downloaded = self._get_latest_video_download()
                if downloaded:
                    dest = os.path.join(videos_dir, f"Frame{i}.mp4")
                    shutil.move(downloaded, dest)
                    saved_videos.append(dest)
                    print(f"  ✅ Video {i} saved: {dest}")
                else:
                    print(f"  ⚠️  Auto-download failed. Save manually.")

            except Exception as e:
                print(f"  ❌ Frame {i} animation failed: {e}")

        return saved_videos

    def _get_latest_video_download(self, timeout=30) -> str | None:
        start = time.time()
        while time.time() - start < timeout:
            files = glob.glob(os.path.join(DOWNLOAD_DIR, "*.mp4"))
            if files:
                newest = max(files, key=os.path.getmtime)
                if time.time() - os.path.getmtime(newest) < 15:
                    return newest
            time.sleep(1)
        return None

    def close(self):
        if self.driver:
            self.driver.quit()
```

---

## Pipeline #3: FFmpeg Cinematic Join

### What it automates:
- Takes all clips from project/videos/
- Applies your standard: fadeblack 0.8s, color grade, fade in/out
- Exports 1080x1920, 24fps, CRF 17
- Saves to project/output/

### Code: `pipelines/ffmpeg_join.py`

```python
"""
FFmpeg Cinematic Join — Your exact pipeline automated.
Fadeblack 0.8s | Warm desaturated grade | CRF 17 | 1080x1920
"""

import os
import subprocess
import glob


def join_reel(project_dir: str, clip_duration: float = 6.041667) -> str:
    """
    Full cinematic join pipeline.

    Steps:
      1. Scale + color grade each clip
      2. Join with fadeblack transitions
      3. Fade in/out from black
      4. Export final reel

    Returns:
        Path to final reel MP4
    """
    videos_dir = os.path.join(project_dir, "videos")
    work_dir = os.path.join(project_dir, "work")
    output_dir = os.path.join(project_dir, "output")
    os.makedirs(work_dir, exist_ok=True)

    # Find all frame clips, sorted
    clips = sorted(glob.glob(os.path.join(videos_dir, "Frame*.mp4")))
    n = len(clips)

    if n == 0:
        print("  ❌ No clips found in videos/")
        return ""

    print(f"  🎬 Processing {n} clips...")

    # Step 1: Scale + Color grade each clip
    for i, clip in enumerate(clips):
        out = os.path.join(work_dir, f"c{i+1}.mp4")
        subprocess.run([
            "ffmpeg", "-y", "-i", clip,
            "-vf", ",".join([
                "scale=1080:1920:flags=lanczos",
                "eq=contrast=1.15:saturation=0.75:brightness=0.02",
                "colorbalance=rs=0.06:gs=0.02:bs=-0.04:rm=0.04:gm=0.01:bm=-0.03"
            ]),
            "-c:v", "libx264", "-profile:v", "high", "-crf", "17",
            "-pix_fmt", "yuv420p", "-r", "24",
            "-c:a", "aac", "-b:a", "192k",
            out
        ], capture_output=True)
        print(f"    Clip {i+1}/{n} graded")

    # Step 2: Build FFmpeg filter for join
    trans_dur = 0.8
    inputs = []
    for i in range(n):
        inputs += ["-i", os.path.join(work_dir, f"c{i+1}.mp4")]

    # Video xfade chain
    vf_parts = []
    prev = "[0:v]"
    for j in range(1, n):
        offset = round(j * clip_duration - j * trans_dur, 6)
        out_label = f"[v{j}]" if j < n - 1 else "[vpre]"
        vf_parts.append(f"{prev}[{j}:v]xfade=transition=fadeblack:duration={trans_dur}:offset={offset}{out_label}")
        prev = out_label if j < n - 1 else "[vpre]"

    # Total duration and fade in/out
    total_dur = n * clip_duration - (n - 1) * trans_dur
    fade_out_start = round(total_dur - 1.0, 2)
    vf_parts.append(f"[vpre]fade=t=in:st=0:d=1,fade=t=out:st={fade_out_start}:d=1[vf]")

    # Audio crossfade chain
    af_parts = []
    prev_a = "[0:a]"
    for j in range(1, n):
        out_a = f"[a{j}]" if j < n - 1 else "[apre]"
        af_parts.append(f"{prev_a}[{j}:a]acrossfade=d={trans_dur}{out_a}")
        prev_a = out_a if j < n - 1 else "[apre]"
    af_parts.append(f"[apre]afade=t=in:st=0:d=1,afade=t=out:st={fade_out_start}:d=1[af]")

    filter_complex = ";".join(vf_parts) + ";" + ";".join(af_parts)

    # Step 3: Export final reel
    output_path = os.path.join(output_dir, "reel_final.mp4")

    cmd = [
        "ffmpeg", "-y"
    ] + inputs + [
        "-filter_complex", filter_complex,
        "-map", "[vf]", "-map", "[af]",
        "-c:v", "libx264", "-profile:v", "high", "-crf", "17",
        "-pix_fmt", "yuv420p", "-r", "24",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        print(f"  ✅ Reel exported: {output_path}")
        # Cleanup work dir
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)
    else:
        print(f"  ❌ FFmpeg error: {result.stderr[-500:]}")

    return output_path
```

---

## Pipeline #4: Google Drive Upload

### What it automates:
- Authenticates with Google Drive API
- Creates organized folder structure
- Uploads reel + frames + metadata
- Returns shareable link

### Setup (one-time):
1. Go to https://console.cloud.google.com
2. Create project → Enable Google Drive API
3. Create OAuth 2.0 credentials → Download `credentials.json`
4. Place `credentials.json` in `~/LotusAgent/`

### Code: `pipelines/drive_uploader.py`

```python
"""
Google Drive Uploader — Auto-upload reels with organized folder structure.

Drive Structure:
  TechEngineLab/
  ├── Reels/
  │   ├── Defense/
  │   │   ├── 2026-04-16_Agni6/
  │   │   │   ├── reel_final.mp4
  │   │   │   ├── frames/
  │   │   │   └── metadata.json
  │   │   └── 2026-04-17_Surya/
  │   └── Universe/
  ├── Carousels/
  └── YouTube/
"""

import os
import json
import mimetypes
from pathlib import Path
from datetime import datetime

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CREDS_FILE = os.path.expanduser("~/LotusAgent/credentials.json")
TOKEN_FILE = os.path.expanduser("~/LotusAgent/token.json")

# Your Drive folder structure
ROOT_FOLDER = "TechEngineLab"
PILLARS = {
    "Defense": None,   # Folder IDs populated on first run
    "Universe": None,
    "DeepSea": None,
    "Wildlife": None,
    "AI": None,
    "Tech": None,
    "Physics": None,
    "Math": None,
}


class DriveUploader:
    def __init__(self):
        self.service = None
        self.folder_ids = {}
        self._authenticate()

    def _authenticate(self):
        """OAuth2 authentication (opens browser on first run)."""
        creds = None

        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(CREDS_FILE):
                    print("  ❌ credentials.json not found!")
                    print("     Get it from: https://console.cloud.google.com")
                    print(f"     Place at: {CREDS_FILE}")
                    return

                flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())

        self.service = build("drive", "v3", credentials=creds)
        print("  ✅ Google Drive authenticated")
        self._ensure_folder_structure()

    def _ensure_folder_structure(self):
        """Create root + pillar folders if they don't exist."""
        # Find or create root
        root_id = self._find_or_create_folder(ROOT_FOLDER)

        # Content type folders
        reels_id = self._find_or_create_folder("Reels", parent_id=root_id)
        carousels_id = self._find_or_create_folder("Carousels", parent_id=root_id)
        youtube_id = self._find_or_create_folder("YouTube", parent_id=root_id)

        self.folder_ids["root"] = root_id
        self.folder_ids["Reels"] = reels_id
        self.folder_ids["Carousels"] = carousels_id
        self.folder_ids["YouTube"] = youtube_id

        # Pillar subfolders under Reels
        for pillar in PILLARS:
            pid = self._find_or_create_folder(pillar, parent_id=reels_id)
            self.folder_ids[f"Reels/{pillar}"] = pid

    def upload_project(self, project_dir: str, pillar: str) -> dict:
        """
        Upload entire project to Drive.

        Returns:
            {"folder_url": str, "reel_url": str, "files_uploaded": int}
        """
        if not self.service:
            return {"error": "Not authenticated"}

        project_name = os.path.basename(project_dir)
        pillar_folder_id = self.folder_ids.get(f"Reels/{pillar}")

        if not pillar_folder_id:
            pillar_folder_id = self.folder_ids["Reels"]

        # Create project folder on Drive
        project_folder_id = self._find_or_create_folder(
            project_name, parent_id=pillar_folder_id
        )
        frames_folder_id = self._find_or_create_folder(
            "frames", parent_id=project_folder_id
        )

        uploaded = 0

        # Upload reel
        reel_path = os.path.join(project_dir, "output", "reel_final.mp4")
        if os.path.exists(reel_path):
            self._upload_file(reel_path, project_folder_id)
            uploaded += 1
            print(f"  ☁️  Uploaded: reel_final.mp4")

        # Upload frames
        frames_dir = os.path.join(project_dir, "frames")
        if os.path.isdir(frames_dir):
            for f in sorted(os.listdir(frames_dir)):
                fpath = os.path.join(frames_dir, f)
                if os.path.isfile(fpath):
                    self._upload_file(fpath, frames_folder_id)
                    uploaded += 1
            print(f"  ☁️  Uploaded: {uploaded - 1} frames")

        # Upload metadata
        meta_path = os.path.join(project_dir, "metadata.json")
        if os.path.exists(meta_path):
            self._upload_file(meta_path, project_folder_id)
            uploaded += 1

        folder_url = f"https://drive.google.com/drive/folders/{project_folder_id}"
        print(f"  ✅ Drive upload complete: {uploaded} files")
        print(f"  🔗 {folder_url}")

        return {
            "folder_url": folder_url,
            "folder_id": project_folder_id,
            "files_uploaded": uploaded,
        }

    def _upload_file(self, filepath: str, parent_id: str) -> str:
        """Upload a single file. Returns file ID."""
        filename = os.path.basename(filepath)
        mime, _ = mimetypes.guess_type(filepath)
        mime = mime or "application/octet-stream"

        metadata = {
            "name": filename,
            "parents": [parent_id],
        }
        media = MediaFileUpload(filepath, mimetype=mime, resumable=True)
        file = self.service.files().create(
            body=metadata, media_body=media, fields="id"
        ).execute()

        return file.get("id")

    def _find_or_create_folder(self, name: str, parent_id: str = None) -> str:
        """Find existing folder or create new one."""
        query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        if parent_id:
            query += f" and '{parent_id}' in parents"

        results = self.service.files().list(
            q=query, spaces="drive", fields="files(id)"
        ).execute()

        files = results.get("files", [])
        if files:
            return files[0]["id"]

        # Create
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            metadata["parents"] = [parent_id]

        folder = self.service.files().create(
            body=metadata, fields="id"
        ).execute()

        return folder.get("id")
```

---

## Pipeline #5: Claude.ai Reel Scoring

### What it automates:
- Opens Claude.ai
- Pastes reel concept + metadata
- Gets 7-factor score (/70)
- Saves score to project metadata

### Code: `pipelines/claude_scorer.py`

```python
"""
Claude.ai Reel Scorer — Routes scoring request to Claude.ai Pro.
Uses browser automation to get free scoring via your Pro subscription.
"""

import time
from router import HybridRouter


SCORING_TEMPLATE = """Score this reel concept using the 7-factor formula (/70):

TOPIC: {topic}
PILLAR: {pillar}
HOOK: {hook}
FRAMES: {frame_count}
VISUAL: {visual_description}

Score each factor 1-10:
1. Trending news within 48hrs
2. India+DRDO/ISRO pride factor
3. Impossible number present
4. Visual action (SEE+MOVING+DESTROYING)
5. High retention (each frame = new question)
6. DM share trigger strength
7. Emotional story arc (pain→struggle→triumph)

Give total /70 and one-line verdict: POST, HOLD, or CAROUSEL INSTEAD."""


def score_reel(topic: str, pillar: str, hook: str,
               frame_count: int, visual_description: str) -> str:
    """Send scoring request to Claude.ai via browser."""
    router = HybridRouter()

    prompt = SCORING_TEMPLATE.format(
        topic=topic,
        pillar=pillar,
        hook=hook,
        frame_count=frame_count,
        visual_description=visual_description,
    )

    result = router.send_to_claude_chat(prompt)
    return result
```

---

## Master Orchestrator — Ties Everything Together

### Code: `pipelines/orchestrator.py`

```python
"""
Master Orchestrator — Full pipeline triggered by single voice command.

"Lotus, create defense reel for Agni-6"
  → NB2 generates 5 frames
  → Grok animates each
  → FFmpeg joins cinematically
  → Claude.ai scores
  → Google Drive uploads
  → Voice reports result
"""

import json
import os
from datetime import datetime
from nb2_generator import NB2Generator
from grok_animator import GrokAnimator
from ffmpeg_join import join_reel
from drive_uploader import DriveUploader
from claude_scorer import score_reel


# ═══════════════════════════════════════════════
#  PROMPT TEMPLATES — Your reel production rules
# ═══════════════════════════════════════════════

REEL_TEMPLATES = {
    "Defense": {
        "nb2_style": (
            "Photorealistic military scene, {subject}, "
            "cinematic lighting, desert battlefield, dust particles, "
            "white bold military stencil text with heavy black drop shadow, "
            "no colored panels, bottom 15 percent kept clear, "
            "dramatic composition, 1080x1920 vertical"
        ),
        "grok_style": (
            "{subject} perfectly steady and stable, "
            "{motion_description}, still camera, 6 seconds."
        ),
        "frame_structure": [
            "F1_ACTION",     # Scroll-stopper
            "F2_3DRENDER",   # Specs
            "F3_ACTION",     # Escalation
            "F4_ACTION",     # Combat proof
            "F5_DATACARD",   # Data card
        ],
    },
}


def run_full_pipeline(
    topic: str,
    pillar: str,
    nb2_prompts: list[str],
    grok_prompts: list[str],
    hook: str = "",
    visual_description: str = "",
    skip_grok: bool = False,
    skip_score: bool = False,
    skip_upload: bool = False,
) -> dict:
    """
    Execute the complete reel production pipeline.

    Args:
        topic: Reel topic (e.g., "Agni-6")
        pillar: Content pillar (e.g., "Defense")
        nb2_prompts: List of NB2 image prompts per frame
        grok_prompts: List of Grok animation prompts per frame
        hook: First-frame hook text
        visual_description: Brief visual description for scoring
        skip_grok: Skip Grok animation (use static frames)
        skip_score: Skip Claude.ai scoring
        skip_upload: Skip Google Drive upload

    Returns:
        Pipeline result dict with all paths and status
    """
    result = {
        "topic": topic,
        "pillar": pillar,
        "started": datetime.now().isoformat(),
        "steps": {},
    }

    # ── Step 1: NB2 Image Generation ──────────────────
    print("\n" + "=" * 50)
    print(f"  PIPELINE: {topic} ({pillar})")
    print("=" * 50)

    nb2 = NB2Generator()
    project_dir = nb2.create_project(topic, pillar)
    result["project_dir"] = project_dir

    print("\n  📸 STEP 1: NB2 Frame Generation")
    frames = nb2.generate_frames(nb2_prompts)
    result["steps"]["nb2"] = {"frames": frames, "count": len(frames)}
    nb2.close()

    if not frames:
        result["error"] = "NB2 generation failed"
        return result

    # ── Step 2: Grok Animation ────────────────────────
    if not skip_grok:
        print("\n  🎬 STEP 2: Grok Animation")
        grok = GrokAnimator(project_dir)
        videos = grok.animate_frames(frames, grok_prompts)
        result["steps"]["grok"] = {"videos": videos, "count": len(videos)}
        grok.close()
    else:
        print("\n  ⏭️  STEP 2: Grok skipped")

    # ── Step 3: FFmpeg Join ───────────────────────────
    print("\n  🎞️  STEP 3: FFmpeg Cinematic Join")
    reel_path = join_reel(project_dir)
    result["steps"]["ffmpeg"] = {"reel": reel_path}

    # ── Step 4: Claude.ai Score ───────────────────────
    if not skip_score:
        print("\n  📊 STEP 4: Claude.ai Scoring")
        score = score_reel(
            topic=topic,
            pillar=pillar,
            hook=hook,
            frame_count=len(frames),
            visual_description=visual_description,
        )
        result["steps"]["score"] = score
    else:
        print("\n  ⏭️  STEP 4: Scoring skipped")

    # ── Step 5: Google Drive Upload ───────────────────
    if not skip_upload:
        print("\n  ☁️  STEP 5: Google Drive Upload")
        drive = DriveUploader()
        upload_result = drive.upload_project(project_dir, pillar)
        result["steps"]["drive"] = upload_result
    else:
        print("\n  ⏭️  STEP 5: Upload skipped")

    # ── Done ──────────────────────────────────────────
    result["completed"] = datetime.now().isoformat()
    result["status"] = "success"

    # Save result to project
    with open(os.path.join(project_dir, "pipeline_result.json"), "w") as f:
        json.dump(result, f, indent=2)

    print("\n" + "=" * 50)
    print(f"  ✅ PIPELINE COMPLETE: {topic}")
    print(f"  📁 {project_dir}")
    if "drive" in result["steps"]:
        print(f"  🔗 {result['steps']['drive'].get('folder_url', '')}")
    print("=" * 50 + "\n")

    return result
```

---

## Voice Integration — How Gemma 4 Triggers Pipelines

### Add to LOTUS Agent's tool list:

```python
# In agent.py — add these tools for Gemma 4 to call

{"name": "create_reel", "description": "Start full reel production pipeline",
 "input_schema": {"type": "object", "properties": {
   "topic": {"type": "string"},
   "pillar": {"type": "string", "enum": ["Defense","Universe","DeepSea","Wildlife"]},
   "nb2_prompts": {"type": "array", "items": {"type": "string"}},
   "grok_prompts": {"type": "array", "items": {"type": "string"}},
 }, "required": ["topic", "pillar"]}},

{"name": "upload_to_drive", "description": "Upload project to Google Drive",
 "input_schema": {"type": "object", "properties": {
   "project_dir": {"type": "string"},
   "pillar": {"type": "string"}
 }, "required": ["project_dir", "pillar"]}},

{"name": "score_reel", "description": "Score reel concept via Claude.ai",
 "input_schema": {"type": "object", "properties": {
   "topic": {"type": "string"},
   "pillar": {"type": "string"},
   "hook": {"type": "string"},
   "visual_description": {"type": "string"}
 }, "required": ["topic"]}},

{"name": "join_clips", "description": "FFmpeg cinematic join of video clips",
 "input_schema": {"type": "object", "properties": {
   "project_dir": {"type": "string"}
 }, "required": ["project_dir"]}},

{"name": "list_projects", "description": "List all reel projects",
 "input_schema": {"type": "object", "properties": {}}},
```

### Example voice commands:

```
"Lotus, create a defense reel for Agni-6"
  → Gemma understands: topic=Agni-6, pillar=Defense
  → Triggers create_reel tool
  → Full pipeline runs automatically

"Lotus, just upload today's project to Drive"
  → Triggers upload_to_drive for latest project

"Lotus, score my Surya reel concept"
  → Opens Claude.ai, pastes scoring template

"Lotus, join the clips in my current project"
  → FFmpeg join only (skip NB2/Grok)

"Lotus, what projects did I work on today?"
  → Lists ~/LotusAgent/Projects/ by date
```

---

## Installation

### One-time setup on Mac Studio:

```bash
# 1. Python dependencies
pip install selenium google-auth-oauthlib google-api-python-client

# 2. ChromeDriver (for browser automation)
brew install chromedriver

# 3. FFmpeg (you probably have this)
brew install ffmpeg

# 4. Ollama + Gemma 4
brew install ollama
ollama pull gemma4:26b-a4b

# 5. Google Drive credentials
# → https://console.cloud.google.com
# → Create project → Enable Drive API
# → Create OAuth credentials → Download credentials.json
# → Place at ~/LotusAgent/credentials.json

# 6. Create project structure
mkdir -p ~/LotusAgent/{Projects,pipelines}
```

---

## Folder Structure

```
~/LotusAgent/
├── agent.py              ← Main agent (voice + brain)
├── voice.py              ← STT + TTS
├── tools.py              ← Screen control tools
├── router.py             ← Hybrid router
├── auth.py               ← Voice authentication
├── server.py             ← Dashboard WebSocket
├── dashboard.html         ← JARVIS UI
├── credentials.json       ← Google Drive OAuth
├── token.json             ← Auto-generated auth token
│
├── pipelines/
│   ├── nb2_generator.py   ← NB2 browser automation
│   ├── grok_animator.py   ← Grok video automation
│   ├── ffmpeg_join.py     ← Cinematic join
│   ├── drive_uploader.py  ← Google Drive upload
│   ├── claude_scorer.py   ← Reel scoring via Claude.ai
│   └── orchestrator.py    ← Master pipeline
│
└── Projects/
    ├── 2026-04-16_Defense_Agni6/
    │   ├── frames/
    │   ├── videos/
    │   ├── output/
    │   └── metadata.json
    └── 2026-04-17_Universe_BlackHole/
```

---

## Phase 2: Fine-Tuning Gemma 4 (Later)

Once the pipelines are working, you can fine-tune Gemma 4 with LoRA
so it understands your specific vocabulary without long system prompts:

- Pillar names and schedules
- Your hashtag rules
- NB2 prompt style preferences
- "Make it like the Shaurya reel" → knows your top performer format
- Carousel vs Reel decision based on topic

Fine-tuning E4B (4B) on M1 Max 32GB:
  - Unsloth + QLoRA = ~8GB VRAM
  - 100-200 training examples from your conversations
  - 30 minutes training time
  - Model learns YOUR content brain

This is Phase 2 — get the automation working first.
