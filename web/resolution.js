// Width x height under the video, the way ComfyUI already draws it under an
// image preview.
//
// Core draws that readout in renderPreview() on the canvas, but only for image
// previews: it reserves a 15px band under the picture and fillText()s
// "1024 x 576" into it. A video preview is not canvas-drawn at all — core's
// useNodeVideo() puts a real <video> in a DOM widget — so nothing in that path
// ever reaches the fillText, and the one number you most want when judging a
// video preview is the one thing the node does not tell you.
//
// This adds the same line for PreviewWebM by appending a caption element to
// core's video container and reserving the same 15px for it. Matching the image
// readout is the whole point, so the font, the colour, the band height and the
// "W x H" wording are all copied from renderPreview, and the caption honours
// the same Comfy.Node.AllowImageSizeDraw setting that governs the image one.
//
// The caption has to be re-attached rather than added once: core's onLoaded
// does container.replaceChildren(video) on every run, which wipes it. A
// MutationObserver on the container puts it back, and also covers the fact that
// the container does not exist until the first preview has finished loading.

import { app } from "../../scripts/app.js"

const NODE_TYPE = "PreviewWebM"
// The DOM widget core adds in useNodeVideo(); its computeLayoutSize is what
// decides how tall the video area is.
const WIDGET_NAME = "video-preview"
const CAPTION_CLASS = "previewwebm-resolution"
// Same band renderPreview() reserves for the image readout.
const CAPTION_HEIGHT = 15
const SIZE_SETTING = "Comfy.Node.AllowImageSizeDraw"

const observedContainers = new WeakSet()
const patchedWidgets = new WeakSet()

function sizeDrawEnabled() {
  return app.ui?.settings?.getSettingValue?.(SIZE_SETTING) ?? true
}

function createCaption() {
  const caption = document.createElement("div")
  caption.className = CAPTION_CLASS
  Object.assign(caption.style, {
    flex: "0 0 auto",
    height: `${CAPTION_HEIGHT}px`,
    lineHeight: "13px",
    fontFamily: "sans-serif",
    fontSize: "10px",
    textAlign: "center",
    // What the canvas readout uses; LiteGraph is global in the frontend.
    color: window.LiteGraph?.NODE_TEXT_COLOR ?? "#AAA",
    pointerEvents: "none",
    overflow: "hidden",
  })
  return caption
}

function updateCaption(container, video) {
  const caption = container.querySelector(`.${CAPTION_CLASS}`)
  if (!caption) return
  caption.style.display = sizeDrawEnabled() ? "" : "none"
  // Empty until the metadata lands; videoWidth is 0 before that.
  caption.textContent = video.videoWidth
    ? `${video.videoWidth} × ${video.videoHeight}`
    : ""
}

// Give the caption its own 15px instead of taking them off the video. The
// widget's own computeLayoutSize closes over dimensions core recomputes on
// every load, so this wraps the function rather than replacing the numbers.
function reserveCaptionSpace(node) {
  const widget = node.widgets?.find((w) => w.name === WIDGET_NAME)
  if (!widget || patchedWidgets.has(widget)) return
  patchedWidgets.add(widget)

  const base = widget.computeLayoutSize?.bind(widget)
  widget.computeLayoutSize = (...args) => {
    const size = base?.(...args) ?? { minHeight: 0, minWidth: 0 }
    if (!sizeDrawEnabled()) return size
    return { ...size, minHeight: (size.minHeight ?? 0) + CAPTION_HEIGHT }
  }
}

function sync(node, container) {
  const video = container.querySelector("video")
  if (!video) return

  reserveCaptionSpace(node)

  // .comfy-img-preview sizes its video to 100% of the container, which in a
  // column layout would leave the caption outside the overflow-hidden box.
  Object.assign(video.style, { flex: "1 1 0", minHeight: "0", height: "auto" })

  if (!container.querySelector(`.${CAPTION_CLASS}`)) {
    container.append(createCaption())
  }
  updateCaption(container, video)

  // Loading happens off-DOM, so the metadata is usually already there by the
  // time the element is inserted — but not on a cached reload of a 404'd temp
  // file, and not if the browser defers the fetch.
  if (!video.dataset.previewwebmBound) {
    video.dataset.previewwebmBound = "1"
    video.addEventListener("loadedmetadata", () => updateCaption(container, video))
  }
}

function observe(node, container) {
  if (observedContainers.has(container)) return
  observedContainers.add(container)

  container.style.flexDirection = "column"
  container.style.flexWrap = "nowrap"

  // Fires on core's replaceChildren, and once more for our own append — which
  // then finds the caption already there and stops.
  new MutationObserver(() => sync(node, container)).observe(container, {
    childList: true,
  })
  sync(node, container)
}

// The container is created lazily by core, inside a function with no hook of
// its own. Intercepting the assignment is the only notification available.
function watchVideoContainer(node) {
  let container = node.videoContainer
  Object.defineProperty(node, "videoContainer", {
    configurable: true,
    enumerable: true,
    get: () => container,
    set(value) {
      container = value
      if (value) observe(node, value)
    },
  })
  if (container) observe(node, container)
}

app.registerExtension({
  name: "dreevelle.PreviewWebM.Resolution",

  nodeCreated(node) {
    if ((node.comfyClass ?? node.type) !== NODE_TYPE) return
    watchVideoContainer(node)
  },

  setup() {
    app.ui?.settings?.addEventListener?.(`${SIZE_SETTING}.change`, () => {
      for (const node of app.graph?.nodes ?? []) {
        if ((node.comfyClass ?? node.type) !== NODE_TYPE) continue
        const container = node.videoContainer
        const video = container?.querySelector("video")
        if (video) updateCaption(container, video)
        node.setDirtyCanvas?.(true, true)
      }
    })
  },
})
