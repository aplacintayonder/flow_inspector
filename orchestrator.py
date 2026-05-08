import json
import os
import subprocess
import pyautogui
import time
import hashlib
import shutil
import datetime
from collections import deque
from PIL import Image, ImageChops, ImageDraw, ImageStat
from pywinauto import Desktop

OUTPUT_DIR = "output"
AI_DIR = "ai"

class UIProvider:
    def __init__(self, script_path: str, app_name: str):
        self.script_path = script_path
        self.app_name = app_name
        self.output_file = None   # json path — resolved after each refresh
        self.image_file  = None   # png path  — resolved after each refresh

    def refresh_layout(self):
        print(f"[*] Refreshing UI map via {self.script_path}...")
        subprocess.run(
            ["python", self.script_path, self.app_name, "--output-dir", OUTPUT_DIR],
            check=True
        )
        # Resolve paths written by this run via latest.txt
        latest_ptr = os.path.join(OUTPUT_DIR, "latest.txt")
        if os.path.exists(latest_ptr):
            with open(latest_ptr, 'r', encoding='utf-8') as f:
                lines = [l.strip() for l in f.read().splitlines() if l.strip()]
            self.output_file = lines[0]
            self.image_file  = lines[1] if len(lines) > 1 else os.path.splitext(lines[0])[0] + ".png"
        else:
            print("[-] latest.txt not found; no elements will be loaded.")
            self.output_file = None
            self.image_file  = None

    @property
    def scan_id(self) -> str | None:
        """Extract the YYYYMMDD_HHMMSS stamp from the current output filename."""
        if not self.output_file:
            return None
        base = os.path.splitext(os.path.basename(self.output_file))[0]  # brave_20260508_212544
        parts = base.rsplit('_', 2)
        return f"{parts[-2]}_{parts[-1]}" if len(parts) >= 3 else base

    def get_elements(self) -> list:
        if not self.output_file or not os.path.exists(self.output_file):
            return []
        with open(self.output_file, 'r', encoding='utf-8') as f:
            return json.load(f)

class InteractionGuard:
    def __init__(self):
        self.visited = set()          # UUIDs seen in current layout scan
        self.visited_sigs = set()     # (name, type) pairs across all layout refreshes
        self.actionable_types = {
            "Button", "MenuItem", "CheckBox", "Hyperlink", "TabItem",
            "Edit", "RadioButton", "ComboBox", "ListItem", "Slider"
        }
        # Robust blacklist to prevent closing the app
        self.forbidden_names = {"Close", "Exit", "Quit", "Terminate", "Restore", "Minimize"}

    def should_process(self, element: dict) -> bool:
        name = str(element.get("name", ""))
        uid = element.get("uuid")
        el_type = element.get("type")
        sig = (name, el_type)

        # Check if any forbidden keyword is in the name (e.g., "Close Tab")
        is_forbidden = any(word in name for word in self.forbidden_names)
        is_new_uid = uid not in self.visited
        is_new_sig = sig not in self.visited_sigs
        is_clickable = el_type in self.actionable_types

        return is_new_uid and is_new_sig and not is_forbidden and is_clickable

    def mark_visited(self, uid: str, name: str, el_type: str):
        self.visited.add(uid)
        self.visited_sigs.add((name, el_type))

# ---------------------------------------------------------------------------
# Session recording — logs every screen + interaction, deduplicates images,
# then assembles the ai/ folder for LLM consumption.
# ---------------------------------------------------------------------------
class SessionRecorder:
    def __init__(self, app_name: str, output_dir: str, ai_dir: str):
        self.app_name   = app_name
        self.output_dir = output_dir
        self.ai_dir     = ai_dir
        self._hash_to_scan: dict[str, str] = {}  # pixel-hash → first scan_id
        self.screens: list[dict] = []             # ordered unique screens

    # -- helpers -------------------------------------------------------------

    def _pixel_hash(self, image_path: str) -> str | None:
        """SHA-1 of raw RGB pixel bytes — ignores PNG metadata/compression."""
        try:
            data = Image.open(image_path).convert("RGB").tobytes()
            return hashlib.sha1(data).hexdigest()
        except Exception:
            return None

    def _log_path(self, scan_id: str) -> str:
        app_base = self.app_name.lower().replace(' ', '_')
        return os.path.join(self.output_dir, f"{app_base}_{scan_id}_log.json")

    def _write_log(self, screen: dict):
        with open(self._log_path(screen["scan_id"]), 'w', encoding='utf-8') as f:
            json.dump(screen, f, indent=4)

    # -- public API ----------------------------------------------------------

    def register_screen(self, scan_id: str | None, image_path: str | None,
                        json_path: str | None, trigger: dict | None = None) -> str | None:
        """
        Called after each refresh_layout(). Returns the effective scan_id to use
        for subsequent log_interaction() calls, or None if the screen is a duplicate
        (files are removed to avoid redundancy).
        """
        if not scan_id or not image_path or not json_path:
            return None

        h = self._pixel_hash(image_path)
        if h and h in self._hash_to_scan:
            dup_id = self._hash_to_scan[h]
            print(f"    [dedup] Screen identical to scan {dup_id} — removing duplicate files.")
            for path in (image_path, json_path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            return None   # caller should not log interactions for this screen

        if h:
            self._hash_to_scan[h] = scan_id

        # Count elements for metadata
        elem_count = 0
        try:
            with open(json_path, encoding='utf-8') as f:
                elem_count = len(json.load(f))
        except Exception:
            pass

        screen = {
            "scan_id":        scan_id,
            "app":            self.app_name,
            "timestamp":      datetime.datetime.now().isoformat(),
            "image_path":     image_path,
            "json_path":      json_path,
            "trigger":        trigger,
            "element_count":  elem_count,
            "interactions":   [],
        }
        self.screens.append(screen)
        self._write_log(screen)
        print(f"    [recorder] Registered screen {len(self.screens)} (scan {scan_id})")
        return scan_id

    def log_interaction(self, scan_id: str, element: dict):
        """Append an interaction record to the matching screen log."""
        for screen in self.screens:
            if screen["scan_id"] == scan_id:
                screen["interactions"].append({
                    "type":        element["type"],
                    "name":        element["name"],
                    "action":      "type" if element["type"] == "Edit" else "click",
                    "coordinates": element["coordinates"],
                })
                self._write_log(screen)
                break

    def build_ai_folder(self):
        """Copy unique screenshots into ai/ and write a consolidated flow.json."""
        os.makedirs(self.ai_dir, exist_ok=True)
        flow = {
            "app":                 self.app_name,
            "session_date":        datetime.datetime.now().isoformat(),
            "total_unique_screens": len(self.screens),
            "screens":             [],
        }
        for i, screen in enumerate(self.screens, 1):
            ext = os.path.splitext(screen["image_path"])[1]
            ai_img = os.path.join(self.ai_dir, f"screen_{i:03d}{ext}")
            try:
                shutil.copy2(screen["image_path"], ai_img)
            except OSError as e:
                print(f"    [ai] Could not copy image: {e}")
                ai_img = screen["image_path"]

            flow["screens"].append({
                "screen_index":          i,
                "scan_id":               screen["scan_id"],
                "timestamp":             screen["timestamp"],
                "image":                 ai_img,
                "trigger":               screen["trigger"],
                "element_count":         screen["element_count"],
                "interactions_performed": screen["interactions"],
            })

        flow_path = os.path.join(self.ai_dir, "flow.json")
        with open(flow_path, 'w', encoding='utf-8') as f:
            json.dump(flow, f, indent=4)
        print(f"\n[AI] Flow saved → {flow_path}  ({len(self.screens)} unique screens)")


class DesktopNavigator:
    def __init__(self, provider: UIProvider, max_explores: int = 0):
        """
        max_explores: maximum number of ui_explorer calls (0 = unlimited).
        The initial scan counts as 1.
        """
        self.provider = provider
        self.guard = InteractionGuard()
        self.max_explores = max_explores

    def _click_globally(self, coords: dict):
        """Uses PyAutoGUI for raw hardware-level clicking to bypass COM errors."""
        x = (coords["left"] + coords["right"]) // 2
        y = (coords["top"] + coords["bottom"]) // 2
        pyautogui.moveTo(x, y, duration=0.2)
        pyautogui.click()
        time.sleep(0.5)

    def _type_in_field(self, coords: dict):
        """Clicks an Edit field and types a test value."""
        x = (coords["left"] + coords["right"]) // 2
        y = (coords["top"] + coords["bottom"]) // 2
        pyautogui.click(x, y)
        time.sleep(0.3)
        pyautogui.hotkey('ctrl', 'a')
        pyautogui.typewrite('test', interval=0.05)
        time.sleep(0.3)

    def _capture_window(self):
        """Captures a screenshot of the target application window. Returns a PIL Image or None."""
        try:
            win = Desktop(backend="uia").window(title_re=f".*{self.provider.app_name}.*")
            if win.exists():
                r = win.element_info.rectangle
                return pyautogui.screenshot(region=(r.left, r.top, r.width(), r.height()))
        except Exception:
            pass
        return None

    def _background_changed(self, before, after, coords: dict, threshold: float = 8.0) -> bool:
        """
        Compares the background region (excluding the bordered element box) between
        two window screenshots. Returns True if the average pixel delta exceeds the threshold.
        """
        if before is None or after is None:
            return False
        if before.size != after.size:
            return True

        diff = ImageChops.difference(before.convert("RGB"), after.convert("RGB"))

        # Mask out the element's own bounding box so its click animation doesn't
        # count as a background change.
        try:
            win = Desktop(backend="uia").window(title_re=f".*{self.provider.app_name}.*")
            if win.exists():
                wr = win.element_info.rectangle
                mask = ImageDraw.Draw(diff)
                box = (
                    max(0, coords["left"] - wr.left),
                    max(0, coords["top"] - wr.top),
                    min(diff.width, coords["right"] - wr.left),
                    min(diff.height, coords["bottom"] - wr.top),
                )
                mask.rectangle(box, fill=(0, 0, 0))
        except Exception:
            pass

        stat = ImageStat.Stat(diff)
        mean_diff = sum(stat.mean) / len(stat.mean)
        print(f"    [bg-check] mean pixel delta = {mean_diff:.2f} (threshold={threshold})")
        return mean_diff > threshold

    def _focus_window(self):
        try:
            win = Desktop(backend="uia").window(title_re=f".*{self.provider.app_name}.*")
            if win.exists():
                win.set_focus()
        except Exception as e:
            print(f"[-] Could not focus window (continuing anyway): {e}")

    def run_session(self):
        explore_count = 0

        def _refresh_or_stop(trigger_info=None):
            """Run refresh_layout and return (scan_id | None, stop_flag)."""
            nonlocal explore_count
            if self.max_explores and explore_count >= self.max_explores:
                print(f"[*] Max explore calls reached ({self.max_explores}). Stopping.")
                return None, True
            self.provider.refresh_layout()
            explore_count += 1
            print(f"[*] Explore call {explore_count}" +
                  (f" / {self.max_explores}" if self.max_explores else "") + ".")
            sid = recorder.register_screen(
                self.provider.scan_id,
                self.provider.image_file,
                self.provider.output_file,
                trigger=trigger_info,
            )
            return sid, False

        self._focus_window()
        recorder = SessionRecorder(self.provider.app_name, OUTPUT_DIR, AI_DIR)

        current_scan_id, stop = _refresh_or_stop()
        if stop:
            recorder.build_ai_folder()
            return

        queue = deque(self.provider.get_elements())

        while queue:
            el = queue.popleft()

            if not self.guard.should_process(el):
                if any(word in el.get("name", "") for word in self.guard.forbidden_names):
                    print(f"[!] Skipping safety-critical element: {el['name']}")
                continue

            el_type = el['type']
            coords  = el['coordinates']
            print(f"[+] Interacting with {el_type}: '{el['name']}' at ({coords['left']}, {coords['top']})")

            before = self._capture_window()

            try:
                if el_type == "Edit":
                    self._type_in_field(coords)
                else:
                    self._click_globally(coords)
                self.guard.mark_visited(el["uuid"], el["name"], el_type)
            except Exception as e:
                print(f"[!] Interaction failed for '{el['name']}': {e}")
                continue

            if current_scan_id:
                recorder.log_interaction(current_scan_id, el)

            after = self._capture_window()

            if self._background_changed(before, after, coords):
                print(f"[!] Background changed after '{el['name']}' — pressing Back and re-exploring...")
                pyautogui.hotkey('alt', 'left')
                time.sleep(1.5)
                self._focus_window()
                trigger_info = {
                    "type":   el_type,
                    "name":   el["name"],
                    "action": "type" if el_type == "Edit" else "click",
                }
                current_scan_id, stop = _refresh_or_stop(trigger_info)
                if stop:
                    break
                new_elements = self.provider.get_elements()
                # Prepend new elements; visited_sigs prevents re-clicking known ones
                queue = deque(new_elements) + queue

        recorder.build_ai_folder()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="GabiBot UI orchestrator")
    parser.add_argument("--app",          default="Brave",  help="Window title to target (default: Brave)")
    parser.add_argument("--max-explores", default=0, type=int,
                        help="Maximum number of ui_explorer calls (0 = unlimited)")
    parser.add_argument("--no-chat",      action="store_true",
                        help="Skip launching the chat UI after the session ends")
    parser.add_argument("--model",        default="gpt-5",
                        help="OpenAI model used by the chat app (default: gpt-5)")
    parser.add_argument("--port",         default=5000, type=int,
                        help="Port for the chat UI (default: 5000)")
    args = parser.parse_args()

    EXPLORER_SCRIPT = "ui_explorer.py"
    navigator = DesktopNavigator(
        UIProvider(EXPLORER_SCRIPT, args.app),
        max_explores=args.max_explores,
    )
    navigator.run_session()

    if not args.no_chat:
        print("\n[*] Launching chat UI …  (Ctrl+C to stop)")
        # Run analyze.py in the same interpreter so the user doesn't need a
        # separate terminal.  sys.argv is patched so analyze.py's own argparse
        # receives the right values.
        import sys as _sys
        _sys.argv = [
            "analyze.py",
            "--flow",  os.path.join(AI_DIR, "flow.json"),
            "--model", args.model,
            "--port",  str(args.port),
        ]
        exec(open("analyze.py").read())  # noqa: S102 — intentional local exec