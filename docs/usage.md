# Usage

1. Enter a directory path in the scan bar (or browse to one)
2. Click **Scan Directory**. Frayme first searches the folder tree for video files, showing how many it has found so far and which folder it's in; on a large drive this step can take a while. It then checks the files in parallel, and results stream in real-time. While a scan runs, only **Cancel** is available; the path box, Browse, Scan, and Recover buttons are disabled until it finishes. Cancel stops the scan within a moment, even mid-way through large files, and keeps the partial results with a note of how far it got.
3. Problematic files are grouped by directory with a status:

   | Status | Meaning |
   |--------|---------|
   | **no_moov** | Missing moov atom; video data exists, but the file is unplayable |
   | **corrupt** | Invalid structure, leading zeros, or no valid container |
   | **truncated** | File cut short (mdat claims more bytes than the file contains) |
   | **empty** | 0-byte file |
   | **error** | Permission or OS-level read error |

   Each row also shows what Frayme found: the codec, resolution and camera where it can tell, and a **Details** button that expands a list of notes. A caution icon on the button means there's something to know before trusting a recovery, such as a file whose orientation, frame rate and sound were stored in the missing index. Opening the notes of a damaged MP4/MOV also looks through the whole file and reports how much of it is still video (carved files are often mostly other data). Once recovered, the notes say where the rotation and frame rate came from, how many frames were kept, and anything left to check. Sort the files in each folder by name, size or status with the picker next to the filters.

   HEIC/HEIF/AVIF photos saved with a video extension (common inside Photos libraries) are recognized as still images and skipped, not reported as damaged videos.

4. Once the scan finishes, click **Recover** on any file, and Frayme auto-selects a compatible donor from healthy files found during the scan. Recovered videos are saved to the folder shown next to **Save recovered to** (your Downloads folder unless you pick another with **Browse**; **Show** opens it). Clicking a finished job in the Recovery Queue shows its file in Finder. Damaged files lose their rotation, so each row has a rotation picker: **Auto** copies it from a matching donor, or pick one before recovering; on a recovered row, changing it rewrites the recovered file's rotation right away. Recoveries run two at a time in the **Recovery Queue** panel (bottom right), which shows each job's progress; **Clear Done** removes finished and failed jobs, and is greyed out until something has finished. If a recovery fails, the row shows the reason and the button changes to **Retry**
5. Export results to CSV at any time; it includes what Frayme found about each file
