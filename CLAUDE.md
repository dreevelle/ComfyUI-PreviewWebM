# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file ComfyUI custom node package. All code lives in `__init__.py`;
there is no build step, test suite, or linter config. The whole extension is one
node, `PreviewWebM`, which encodes an image batch to a temporary VP9/WebM file
and shows it as a scrubable video inside the node.

The node is registered through ComfyUI's **V3 schema API**, not the legacy
`NODE_CLASS_MAPPINGS` dict: `comfy_entrypoint()` returns a `ComfyExtension`
whose `get_node_list()` yields `IO.ComfyNode` subclasses. Inputs are declared in
`define_schema()` and arrive as named kwargs to `execute()`; the hidden
`unique_id` is read off `cls.hidden`, not from the signature.

## Local environment

- ComfyUI checkout: `/home/dreevelle/comfy/ComfyUI`
- Python venv (has `av`, `torch`, `torchaudio`): `/home/dreevelle/comfy-env/bin/python`
- During development this tree is symlinked into
  `/home/dreevelle/comfy/ComfyUI/custom_nodes/ComfyUI-PreviewWebM`, so edits are
  live after a server restart. A registry install would instead be an
  independent copy under the registry id `comfyui-preview-webm`.

Run ComfyUI:

```bash
/home/dreevelle/comfy-env/bin/python /home/dreevelle/comfy/ComfyUI/main.py
```

## Verifying changes

**The project's core invariant is the UI payload contract.** The frontend
decides between a `<video>` element and an animated-image widget with two
predicates, and all of the following must hold or the preview silently
degrades:

- `animated` is a truthy **array**, i.e. `(True,)`. The frontend calls
  `output.animated.find(Boolean)`; a bare `True` has no `.find` and breaks.
- No filename in the payload ends in `.png` or `.webp`.
- The payload key is `images`, even though the contents are a video.
- `type` is `temp` and the file really is in the temp directory — the type field
  is what `/view` uses to resolve the base directory.
- `is_output_node=True`, or the node is pruned from the graph and the `ui`
  payload is never emitted at all.

`UI.PreviewVideo` produces the first three for free. Exercise the encoder
offline, without a server:

```bash
cd /home/dreevelle/comfy/ComfyUI && PYTHONPATH=/home/dreevelle/comfy/ComfyUI \
/home/dreevelle/comfy-env/bin/python -c "
import importlib.util, torch, av, os, folder_paths
spec = importlib.util.spec_from_file_location('pw', '/home/dreevelle/Projects/ComfyUI-PreviewWebM/__init__.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
class H: unique_id = 'test'
m.PreviewWebM.hidden = H()
d = m.PreviewWebM.execute(images=torch.rand(24, 128, 192, 3), fps=24.0, quality='fast').ui.as_dict()
print(d['animated'], d['images'][0])
p = os.path.join(folder_paths.get_temp_directory(), d['images'][0]['filename'])
with av.open(p) as c: print({s.type: s.codec_context.name for s in c.streams})
"
```

`execute()` reads `cls.hidden.unique_id`, so a direct call needs `hidden` stubbed
as above. Benchmark on smooth, structured content — pure `torch.rand` is
incompressible white noise, worst case for any codec, and gives numbers that say
nothing about real workloads.

## Architecture

Four layers in `__init__.py`, in order:

1. **`_PRESETS`** — maps the quality combo to crf, deadline, cpu-used, a keyframe
   interval in seconds, and a chroma format. Three settings are load-bearing and
   easy to break:
   - `stream.bit_rate = 0`, without which libvpx-vp9 treats `crf` as constrained
     quality rather than a pure quality target;
   - `gop_size`, which bounds how far a scrub-bar drag has to seek back;
   - the **colour tags and the explicit `reformat` in the encode loop, which must
     agree**. The decoder inverts using the tags, so tags that disagree with the
     conversion are worse than no tags at all. Do not "simplify" the `reformat`
     away by passing the frame straight to `stream.encode()`: the implicit
     reformat there falls back to swscale's defaults, which is BT.601 with no
     matrix tag written — measured at up to a 15% hue error once a browser
     assumes BT.709. `cpu-used` is *not* load-bearing for quality; at a fixed crf
     it trades encode time for file size, and file size is free here.
2. **Audio** — `_opus_sample_rate`, `_prepare_audio`, `_add_audio_stream`,
   `_mux_audio`. WebM accepts only Vorbis and Opus, so the AAC that core's MP4
   path uses is not an option. The rate ladder mirrors `AudioSaveHelper` in
   `comfy_api/latest/_ui.py`. The `mute` widget short-circuits the whole layer by
   forcing `prepared_audio` to `None`, which every function below it already
   handles — keep that check *before* `_prepare_audio`, not after, so muting also
   skips the resample that can raise on a missing `torchaudio`.
3. **`_recycle`** — deletes the file this node wrote last run, keyed by
   `unique_id`.
4. **`PreviewWebM`** — schema and encode loop.

### Three ordering traps, all already hit

- **The audio stream must be declared before the first packet is muxed.**
  Adding it after the container has written its header leaves it with a zero
  time base, and the first `mux` fails with `Cannot rebase to zero time`. Core's
  `VideoFromComponents.save_to` declares both streams up front for this reason.
  Encoding video first and audio last is fine; *declaring* it late is not.
- **A fallback channel layout must narrow the data too.** PyAV validates the
  array's channel count against the layout, so naming `stereo` for a 3-channel
  waveform raises. `_prepare_audio` slices the waveform to match.
- **New widgets go at the *end* of `inputs`, never in the middle.** The frontend
  serialises widget values as a positional `widgets_values` array with no names
  in it, so inserting a widget above an existing one re-points every saved value
  below it. `mute` was first declared between `fps` and `quality`; saved
  workflows stored `[fps, quality]`, so `mute` was handed the quality *string* —
  truthy — and every pre-existing workflow silently lost its audio. Sockets
  (`images`, `audio`) are not widgets and do not occupy slots, so only widget
  order matters. Nothing in a schema-only test catches this; the check is that
  the widget list's leading entries still match what old workflows stored.

### Deliberate design decisions

Recorded so they don't get "fixed" later:

- **No output socket.** `PreviewImage`, `PreviewAudio` and core's
  `SAM3_TrackPreview` all return nothing. `SaveWEBM` passes images through only
  because it is a *Save* node. Giving a preview node an output invites splicing
  it mid-chain, where — being an output node — it would VP9-encode every
  intermediate batch that passes through. Fanning out two links from the source
  costs one drag.
- **`mute` drops the audio stream instead of muting the player.** Muting the
  `<video>` element would be the more obvious reading, but the player is core's
  widget and this package ships no frontend — there is no `web/` directory and
  adding one to set a single attribute would be the largest structural change in
  the repo. Dropping the stream also makes the muted encode strictly cheaper,
  and the preview is throwaway, so nothing is lost by not carrying the track.

- **No `fingerprint_inputs` or `validate_inputs`.** Every input is either an
  upstream tensor already covered by the engine's cache key, or a widget.
- **No `prompt` / `extra_pnginfo` metadata.** This is a throwaway temp file, not
  a save, which also sidesteps `args.disable_metadata`.
- **uuid filenames, not a stable name per node.** Node ids collide across
  workflows, and overwriting a file the browser is mid-range-request on is a
  torn-read hazard. Deleting the *previous* file is safe: the frontend has
  already moved to the new cache-busted URL, and an in-flight response holds the
  old inode open.

### Alpha

RGBA batches encode as `yuva420p` and the container is tagged `alpha_mode=1`.
VP9 alpha rides in a separate BlockAdditional channel, so **a decoder will
report the main plane as `yuv420p`** — that is expected, not a regression. Assert
on the `alpha_mode` metadata tag, not on the decoded pixel format.

This also means **RGBA never takes the 4:4:4 path**, even on `near-lossless`:
libvpx-vp9 offers `yuva420p` and there is no `yuva444p`, so `pix_fmt` falls back
to 4:2:0 whenever `save_alpha` is set. The `alpha_mode` tag is itself the proof
the fallback worked — the matroska muxer only writes it for an alpha pix_fmt.

## Conventions

- Version is duplicated in `__version__` (`__init__.py`) and `[project].version`
  (`pyproject.toml`) — bump both.
- `dependencies` stays empty: `av` and `torch` ship with ComfyUI. `torchaudio`
  is imported lazily and only for resampling, so it stays a soft dependency —
  core guards it the same way.
- The output must stay a single file with a `.webm` extension in the temp
  directory. Each of those three is part of the frontend contract above.

## Releasing

Published on the Comfy Registry as `comfyui-preview-webm` under publisher
`dreevelle`, which is what makes it installable from ComfyUI-Manager.

**Changing `[project].version` in `pyproject.toml` on `main` publishes a
release.** `.github/workflows/publish_action.yml` watches that file's path and
runs `Comfy-Org/publish-node-action`, authenticating with the
`REGISTRY_ACCESS_TOKEN` repo secret. Don't touch the version field unless a
release is intended; republishing an existing version fails with
`400 The node version already exists`.

`.comfyignore` keeps dev-only files (`CLAUDE.md`, `.github/`) out of the
published archive. `comfy node validate` runs the registry's checks locally and
`comfy node pack` produces the exact archive that would be uploaded — inspect it
before releasing if the file list may have changed.

Check what's live:

```bash
curl -s https://api.comfy.org/nodes/comfyui-preview-webm/versions
```
