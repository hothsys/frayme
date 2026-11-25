# Command line

To recover a whole folder without the browser:

```sh
python3 frayme_cli.py --input ~/damaged --output ~/recovered
python3 frayme_cli.py --input ~/damaged --output ~/recovered --donors ~/healthy
python3 frayme_cli.py --input ~/damaged --scan-only
```

| Option | Meaning |
|--------|---------|
| `--input` | Folder to scan, including subfolders (required) |
| `--output` | Folder for recovered videos (required unless `--scan-only`). Subfolders of the input are mirrored under it, so files with the same name in different folders do not collide. It is created if missing |
| `--donors` | Folder of healthy videos from the same camera, like **Choose Donor** in the app |
| `--scan-only` | List what would be recovered and change nothing |

It scans the way the app does and recovers every file the app would offer **Recover** for, two at a time. Each result is decode-tested like the app's, and one line per file is printed with a summary. Your originals are never changed. A recovered file is named `<name>_recovered<ext>`; running again overwrites earlier results in the output folder. The exit status is 0 when nothing failed, 1 when a recovery failed, and 2 when a folder could not be used. It needs the same FFmpeg as the app, and no server is started.
