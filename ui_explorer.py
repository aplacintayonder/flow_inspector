import os
import json
import uuid
import argparse
import random
import datetime
from PIL import Image, ImageDraw
from pywinauto import Desktop
import pyautogui

class UIInspectorSingleOutput:
    def __init__(self, backend="uia"):
        self.backend = backend

    def find_window(self, title_query):
        windows = Desktop(backend=self.backend).windows(title_re=f".*{title_query}.*")
        return windows[0] if windows else None

    def get_random_color(self):
        """Generates a random RGB color for highlighting."""
        return (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))

    def is_meaningful(self, element):
        """Filters for actionable UI elements."""
        info = element.element_info
        rect = info.rectangle
        if rect.width() <= 5 or rect.height() <= 5:
            return False

        interactive_types = {
            "Button", "MenuItem", "Edit", "CheckBox", "RadioButton", 
            "ComboBox", "Hyperlink", "ListItem", "TabItem", "Slider"
        }
        if info.control_type in interactive_types:
            return True
        if info.name and info.name.strip() and info.control_type not in ["Pane", "Grouping"]:
            return True
        return False

    def run(self, app_name, output_dir="output", minify=False):
        # 1. Connect to Window
        win = self.find_window(app_name)
        if not win:
            print(f"[-] Window '{app_name}' not found.")
            return

        win.set_focus()
        rect = win.element_info.rectangle
        
        # 2. Single Screenshot (cropped to window)
        region = (rect.left, rect.top, rect.width(), rect.height())
        full_screenshot = pyautogui.screenshot(region=region)
        draw = ImageDraw.Draw(full_screenshot)

        # 3. Scan Elements
        print(f"[*] Scanning {app_name} for logical elements...")
        final_elements_data = []
        
        for el in win.descendants():
            try:
                if self.is_meaningful(el):
                    info = el.element_info
                    e_rect = info.rectangle

                    # --- Clamp: skip elements whose centre lies outside the window ---
                    cx = (e_rect.left + e_rect.right) // 2
                    cy = (e_rect.top + e_rect.bottom) // 2
                    if not (rect.left <= cx <= rect.right and rect.top <= cy <= rect.bottom):
                        continue
                    
                    color_rgb = self.get_random_color()
                    color_hex = '#%02x%02x%02x' % color_rgb
                    
                    # Keep draw box within the screenshot canvas
                    rel_box = [
                        max(0, e_rect.left - rect.left),
                        max(0, e_rect.top - rect.top),
                        min(full_screenshot.width,  e_rect.right  - rect.left),
                        min(full_screenshot.height, e_rect.bottom - rect.top)
                    ]
                    
                    draw.rectangle(rel_box, outline=color_rgb, width=3)
                    
                    final_elements_data.append({
                        "uuid": str(uuid.uuid4()),
                        "type": info.control_type,
                        "name": info.name or "N/A",
                        "highlight_color": color_hex,
                        "coordinates": {
                            "left": e_rect.left,
                            "top": e_rect.top,
                            "right": e_rect.right,
                            "bottom": e_rect.bottom
                        }
                    })
            except:
                continue

        # 4. Save Outputs – timestamped files inside output_dir
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_base = app_name.lower().replace(' ', '_')
        image_path = os.path.join(output_dir, f"{output_base}_{timestamp}.png")
        json_path  = os.path.join(output_dir, f"{output_base}_{timestamp}.json")

        full_screenshot.save(image_path)
        
        with open(json_path, 'w', encoding='utf-8') as f:
            if minify:
                json.dump(final_elements_data, f, separators=(',', ':'))
            else:
                json.dump(final_elements_data, f, indent=4)

        # Write a pointer so the orchestrator always knows the latest outputs
        latest_ptr = os.path.join(output_dir, "latest.txt")
        with open(latest_ptr, 'w', encoding='utf-8') as f:
            f.write(json_path + "\n")
            f.write(image_path + "\n")

        print(f"\n[SUCCESS]")
        print(f"Total Elements Found: {len(final_elements_data)}")
        print(f"Master Image: {image_path}")
        print(f"Master JSON:  {json_path} {'(minified)' if minify else ''}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("app_name", help="Window title to scan")
    parser.add_argument("--output-dir", default="output", help="Directory to save outputs (default: output)")
    parser.add_argument("--min", action="store_true", help="Export JSON in minified format")
    args = parser.parse_args()

    inspector = UIInspectorSingleOutput()
    inspector.run(args.app_name, output_dir=args.output_dir, minify=args.min)