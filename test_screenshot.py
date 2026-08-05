"""Quick standalone check: can this process actually capture screen pixels?

Run this directly (not via the Flask server) to find out early whether
Screen Recording permission is going to block the whole approach, before
building anything on top of it.
"""
import sys

try:
    import pyautogui
except ImportError:
    print("pyautogui not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

OUT_PATH = "test_screenshot.png"


def main():
    print("Taking screenshot...")
    try:
        img = pyautogui.screenshot()
    except Exception as e:
        print(f"FAILED: screenshot() raised an exception: {e}")
        print("This usually means Screen Recording permission is denied "
              "for the process running this script (e.g. Terminal / python3).")
        sys.exit(1)

    img.save(OUT_PATH)
    print(f"Saved to {OUT_PATH} ({img.width}x{img.height})")

    # Heuristic: a screenshot blocked at the OS level often comes back
    # as a solid black (or solid single-color) image instead of erroring.
    extrema = img.convert("L").getextrema()
    min_val, max_val = extrema
    print(f"Grayscale value range: {min_val}-{max_val}")

    if max_val - min_val < 3:
        print("WARNING: image has almost no variation — this looks like a "
              "blocked/blank capture, not a real screenshot. Check System "
              "Settings > Privacy & Security > Screen Recording for the "
              "process running this (Terminal, iTerm, or python3 itself).")
        sys.exit(2)

    print("Looks like a real screenshot. Screen capture is working for this process.")


if __name__ == "__main__":
    main()
