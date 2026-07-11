export type VideoFormat = "square" | "story";

export type BrandedQuoteProps = {
  // The hook line, rendered large and centered.
  hookText: string;
  // Filename of the background image inside video/public/ (resolved via
  // staticFile). The Python runner copies the Pollinations image there.
  backgroundImagePath: string;
  // "square" 1080x1080 or "story" 1080x1920.
  format: VideoFormat;
  // Show the "@zentraiq" handle bottom-left.
  showHandle: boolean;
};

export const FORMAT_DIMENSIONS: Record<VideoFormat, {width: number; height: number}> = {
  square: {width: 1080, height: 1080},
  story: {width: 1080, height: 1920},
};

export const FPS = 30;
