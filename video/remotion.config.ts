// Remotion CLI config. Kept minimal — H.264 MP4 is the default; we only set the
// image format for stills and ensure overwrites are allowed for repeat renders.
import {Config} from "@remotion/cli/config";

Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
