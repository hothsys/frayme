# Feature overview

- Scans directories recursively for damaged video files (`.mp4`, `.mov`, `.m4v`, `.m4a`, `.3gp`, `.3g2`, `.avi`)
- Detects missing moov atoms, leading-zero corruption, truncated files, empty files, and invalid container structures
- Recovers files using raw H.264 NAL extraction with SPS/PPS probing, or moov transplant from a healthy donor file
- Auto-selects donors by matching filename prefixes, so directories with mixed devices (GoPro, iPhone, Nikon) are handled
- Explains each damaged file: codec, resolution, camera, what the damage cost (orientation, frame rate, sound) and how much of the file is still video
- Exports scan results to CSV
