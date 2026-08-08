# ComfyUI-PreviewWebM

A **scrubable inline video preview** for image batches in
[ComfyUI](https://github.com/comfyanonymous/ComfyUI) — with a real seek bar, and
without saving anything.

ComfyUI has `Preview Image`, but no preview for video. The built-in `SaveWEBM`
and `SaveVideo` nodes write into `output/`, which means every time you queue a
sampler to see how the motion looks, you leave a file behind to clean up later.

This node encodes to the temp directory instead and hands the frontend a preview
descriptor, so the player appears in the node and nothing lands in `output/`. It
also deletes its own previous file each run, so a long tuning session doesn't
quietly fill the disk with discarded takes. Assembling and saving the real thing
stays wherever you already do it.

## Node

**Preview Video (WebM)** (category: `image/video`)

| Input | Default | Notes |
|---|---|---|
| `images` | — | The image batch to play back. |
| `fps` | `16.0` | Playback frame rate, defaulting to what the Wan video models generate at. Stored as an exact rational, so `23.976` stays `2997/125`. |
| `audio` | *(optional)* | Muxed as Opus, trimmed to the length of the image batch. |
| `quality` *(advanced)* | `balanced` | Encoder speed against picture quality. See below. |

No outputs — wire `images` in from the same socket that feeds your save node.
It is an output node, so it runs on its own.

## Performance

libvpx-vp9's defaults are tuned for archival encodes, not for a file you glance
at once and throw away. 60 frames of 1920x1080:

| preset | time | per frame | size |
|---|---:|---:|---:|
| `SaveWEBM` defaults, for comparison | 2.77 s | 46.1 ms | 85 KiB |
| `fast` | 0.67 s | 11.1 ms | 142 KiB |
| `balanced` *(default)* | 1.31 s | 21.8 ms | 109 KiB |
| `high` | 1.40 s | 23.4 ms | 327 KiB |

`fast` is roughly 4x quicker than the built-in node's settings at the cost of a
larger throwaway file. `high` spends its budget on picture quality rather than
compression, which is why it is both slower *and* bigger — use it when you need
to judge fine detail, not to save space.

## Installation

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/dreevelle/ComfyUI-PreviewWebM
```

No extra dependencies; PyAV and torch ship with ComfyUI. `torchaudio` is only
needed if you feed in audio at a sample rate Opus does not support, and ComfyUI
already ships it in practice.

## Notes

- **Nothing is saved.** Files go to ComfyUI's temp directory, which is wiped
  when the server starts and stops. Each preview node keeps exactly one file at
  a time and deletes its previous one on the next run.
- **A preview does not survive a restart.** The player will 404 until you queue
  again — the workflow remembers the result, but the temp file is gone.
- **Scrubbing works** because `/view` serves through a range-capable response.
  Keyframes are placed about once per second (twice per second on `high`), which
  bounds how far a drag has to seek back.
- **Alpha is preserved** when the batch is RGBA: the stream is encoded as
  `yuva420p` and the container is tagged `alpha_mode=1`. Note that the alpha
  rides in a separate channel, so tools inspecting the file will report the main
  plane as `yuv420p` — that is normal, not a loss.
- **Audio must be Opus**, because WebM accepts only Vorbis and Opus, not the AAC
  that ComfyUI's MP4 path uses. Sample rates Opus does not support are resampled
  up to the nearest supported one, matching how core writes `.opus` files.
  Channel counts other than 1, 2 and 6 are narrowed to stereo.
- **Odd dimensions are fine.** Non-even widths and heights encode without any
  padding or cropping.
- **One video per node.** The frontend displays only the first result, so this
  deliberately produces a single file.

## License

[MIT](LICENSE)
