# How recovery works

## Raw NAL Extraction (Primary Strategy)

When a video has raw media data (`mdat`) but is missing its metadata index (`moov`):

1. Scans the mdat for length-prefixed H.264 or HEVC NAL units, skipping the audio interleaved between them and any unrelated data
2. Uses the file's own SPS/PPS parameter sets if it carries them, and collects all unique ones from healthy donor files
3. Probes each candidate by test-decoding against the actual video data to find the correct match; for H.264 with no matching donor, infers the parameters from the frames themselves (trial decoding)
4. Trims to the first IDR keyframe (skips orphaned P/B-frames that cause playback failures)
5. Filters out damaged NALs, and skips frames until the next keyframe wherever reference frames are missing
6. Wraps the clean Annex-B stream with FFmpeg at the correct framerate
7. Decode-tests the result. If it fails as a whole (for example, a carved file holding stretches of another recording), keeps only the groups of pictures that decode cleanly on their own

## Moov Transplant (Fallback)

If raw NAL extraction isn't possible, grafts the moov atom from a healthy donor file and adjusts chunk offset tables to match.

## Leading Zeros Recovery

For files with zeroed-out headers, Frayme tries multiple strategies in order, and no donor is needed unless the parameter sets can't be found or inferred:

1. **Moov-at-end recovery**: if the moov survived at the end of the file, trims damaged chunks and rebuilds (keeps audio)
2. **MP4 structure survival**: if only whole boxes were zeroed (typically the ftyp), replaces them in place so the moov's offsets stay valid (keeps everything)
3. **Raw NAL extraction**: same pipeline as no-moov recovery, over the data after the zeros
4. **Zero strip**: last resort

Every result, from any strategy, must pass the same decode test, so a file that merely looks valid is never reported as recovered.

## Limitations

- Recovery is best-effort. Without a donor, H.264 parameters are inferred from the frames, but HEVC needs a donor from the same camera and mode
- Recovered files from raw extraction are video-only (audio is kept for AVI salvage and when the moov survives)
- Damaged stretches are dropped up to the next keyframe, so recovered videos can have gaps
- A file mixing two recordings (common in carved files) keeps only the parts its parameters decode; without a donor, inference may fail on such files
