import React from "react";
import {Composition} from "remotion";
import {BrandedQuote} from "./BrandedQuote";
import {BrandedQuoteProps, FORMAT_DIMENSIONS, FPS} from "./types";

// Duration scales with hook length: 8s floor, 15s ceiling, ~0.35s per word plus
// a 4s base so short and long hooks both breathe.
const durationSeconds = (hookText: string): number => {
  const words = hookText.trim().split(/\s+/).filter(Boolean).length;
  return Math.max(8, Math.min(15, 4 + words * 0.35));
};

const DEFAULT_PROPS: BrandedQuoteProps = {
  hookText: "Agencies quote 6 months and $200K. We ship in 14 days for $8K.",
  backgroundImagePath: "sample-bg.jpg",
  format: "square",
  showHandle: false,
};

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="BrandedQuote"
      component={BrandedQuote}
      durationInFrames={Math.round(durationSeconds(DEFAULT_PROPS.hookText) * FPS)}
      fps={FPS}
      width={FORMAT_DIMENSIONS.square.width}
      height={FORMAT_DIMENSIONS.square.height}
      defaultProps={DEFAULT_PROPS}
      // Dimensions + duration are derived from the props at render time so one
      // composition serves both formats and any hook length.
      calculateMetadata={({props}) => {
        const dims = FORMAT_DIMENSIONS[props.format] ?? FORMAT_DIMENSIONS.square;
        return {
          ...dims,
          fps: FPS,
          durationInFrames: Math.round(durationSeconds(props.hookText) * FPS),
        };
      }}
    />
  );
};
