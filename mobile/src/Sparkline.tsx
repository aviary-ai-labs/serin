/**
 * SVG price charts — a tiny sparkline for list rows and a full-width area
 * chart for the position detail screen. Pure react-native-svg: no chart
 * library, 60fps-cheap static paths.
 */

import React from "react";
import Svg, { Path, Polyline } from "react-native-svg";

import { useTheme } from "./theme";

type SparkProps = {
  values: number[];
  width?: number;
  height?: number;
  baseline?: number | null;
};

function pointsFor(values: number[], width: number, height: number, pad = 2): string {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  return values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width;
      const y = height - pad - ((value - min) / range) * (height - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

export function Sparkline({ values, width = 72, height = 26, baseline = null }: SparkProps) {
  const theme = useTheme();
  if (!values || values.length < 2) return null;
  const reference = baseline ?? values[0];
  const color = values[values.length - 1] >= reference ? theme.up : theme.down;
  return (
    <Svg width={width} height={height} accessibilityLabel="price sparkline">
      <Polyline points={pointsFor(values, width, height)} fill="none" stroke={color} strokeWidth={1.8} />
    </Svg>
  );
}

type AreaProps = {
  values: number[];
  width: number;
  height?: number;
};

export function AreaChart({ values, width, height = 180 }: AreaProps) {
  const theme = useTheme();
  if (!values || values.length < 2 || width <= 0) return null;
  const up = values[values.length - 1] >= values[0];
  const color = up ? theme.up : theme.down;
  const line = pointsFor(values, width, height, 6);
  const first = line.split(" ")[0];
  const last = line.split(" ").slice(-1)[0];
  const area = `M${first} L${line.split(" ").join(" L")} L${last.split(",")[0]},${height} L${first.split(",")[0]},${height} Z`;
  return (
    <Svg width={width} height={height} accessibilityLabel="price chart">
      <Path d={area} fill={color} opacity={0.12} />
      <Polyline points={line} fill="none" stroke={color} strokeWidth={2.2} />
    </Svg>
  );
}
