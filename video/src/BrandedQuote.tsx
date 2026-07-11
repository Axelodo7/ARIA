import React from "react";
import {
  AbsoluteFill,
  Img,
  staticFile,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {loadFont as loadDMSans} from "@remotion/google-fonts/DMSans";
import {loadFont as loadJetBrainsMono} from "@remotion/google-fonts/JetBrainsMono";
import {BrandedQuoteProps} from "./types";

// Fonts load at module scope so the @font-face rules are injected before render.
const {fontFamily: dmSans} = loadDMSans("normal", {weights: ["400", "700"]});
const {fontFamily: jetBrainsMono} = loadJetBrainsMono("normal", {
  weights: ["400", "500", "700"],
});

const COLORS = {
  bg: "#0A0A0B",
  cyan: "#00F0FF",
  pink: "#FF2D6F",
  white: "#FFFFFF",
  muted: "#B0B5BE",
};

// Hook font size by text length (px, at 1080 wide). Shorter hooks get bigger.
const hookFontSize = (len: number): number => {
  if (len < 40) return 88;
  if (len < 70) return 70;
  if (len < 110) return 56;
  if (len < 160) return 46;
  return 40;
};

export const BrandedQuote: React.FC<BrandedQuoteProps> = ({
  hookText,
  backgroundImagePath,
  format,
  showHandle,
}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames, width} = useVideoConfig();

  // Hook text: fade in over 1s, hold, fade out over the last 1s.
  const fadeIn = interpolate(frame, [0, fps], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const fadeOut = interpolate(
    frame,
    [durationInFrames - fps, durationInFrames],
    [1, 0],
    {extrapolateLeft: "clamp", extrapolateRight: "clamp"},
  );
  const hookOpacity = Math.min(fadeIn, fadeOut);
  // Gentle rise as it fades in.
  const hookTranslateY = interpolate(fadeIn, [0, 1], [24, 0]);

  // URL: subtle pulse (period 2s).
  const urlPulse = 0.7 + 0.3 * Math.sin((2 * Math.PI * frame) / (2 * fps));

  const margin = format === "story" ? 64 : 56;
  const fontSize = hookFontSize(hookText.length);

  return (
    <AbsoluteFill style={{backgroundColor: COLORS.bg}}>
      {/* Background image, covering the frame. */}
      <Img
        src={staticFile(backgroundImagePath)}
        style={{
          position: "absolute",
          width: "100%",
          height: "100%",
          objectFit: "cover",
        }}
      />
      {/* Dark overlay (#0A0A0B ~40%) so text stays readable. */}
      <AbsoluteFill style={{backgroundColor: "rgba(10, 10, 11, 0.4)"}} />
      {/* A touch of extra darkening top and bottom for the chrome text. */}
      <AbsoluteFill
        style={{
          background:
            "linear-gradient(180deg, rgba(10,10,11,0.55) 0%, rgba(10,10,11,0) 22%, " +
            "rgba(10,10,11,0) 78%, rgba(10,10,11,0.55) 100%)",
        }}
      />

      {/* Hook text, centered. */}
      <AbsoluteFill
        style={{
          justifyContent: "center",
          alignItems: "center",
          padding: `0 ${width * 0.09}px`,
        }}
      >
        <div
          style={{
            fontFamily: dmSans,
            fontWeight: 700,
            color: COLORS.white,
            fontSize,
            lineHeight: 1.24,
            textAlign: "center",
            opacity: hookOpacity,
            transform: `translateY(${hookTranslateY}px)`,
            textShadow: "0 2px 24px rgba(0,0,0,0.65)",
            letterSpacing: -0.5,
          }}
        >
          {hookText}
        </div>
      </AbsoluteFill>

      {/* Brand name, top-left. */}
      <div
        style={{
          position: "absolute",
          top: margin,
          left: margin,
          fontFamily: jetBrainsMono,
          fontWeight: 700,
          textTransform: "uppercase",
          color: COLORS.cyan,
          fontSize: 30,
          letterSpacing: 3,
          textShadow: `0 0 14px ${COLORS.cyan}99`,
        }}
      >
        ZentraIQ
      </div>
      {/* Tagline, below brand name. */}
      <div
        style={{
          position: "absolute",
          top: margin + 42,
          left: margin,
          fontFamily: dmSans,
          fontWeight: 400,
          color: COLORS.muted,
          fontSize: 22,
          letterSpacing: 0.5,
        }}
      >
        AI MVPs in 14 days
      </div>

      {/* URL, bottom-right, subtle pulse. */}
      <div
        style={{
          position: "absolute",
          bottom: margin,
          right: margin,
          fontFamily: jetBrainsMono,
          fontWeight: 500,
          color: COLORS.cyan,
          fontSize: 26,
          opacity: urlPulse,
          textShadow: `0 0 12px ${COLORS.cyan}88`,
        }}
      >
        zentraiq.ai
      </div>

      {/* Handle, bottom-left, only when requested. Pink accent dot. */}
      {showHandle ? (
        <div
          style={{
            position: "absolute",
            bottom: margin,
            left: margin,
            fontFamily: jetBrainsMono,
            fontWeight: 400,
            color: COLORS.muted,
            fontSize: 24,
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              backgroundColor: COLORS.pink,
              boxShadow: `0 0 10px ${COLORS.pink}`,
            }}
          />
          @zentraiq
        </div>
      ) : null}
    </AbsoluteFill>
  );
};
