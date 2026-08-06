/**
 * Minimal markdown renderer for briefing output — headings, bullets, bold,
 * inline code and links, which is the full surface the briefing prompt emits.
 * Deliberately dependency-free: a parser for constrained LLM markdown beats
 * shipping a full CommonMark engine for one screen.
 */

import { Linking, StyleSheet, Text, View } from "react-native";
import type { Theme } from "./theme";

type Segment =
  | { kind: "text"; text: string }
  | { kind: "bold"; text: string }
  | { kind: "code"; text: string }
  | { kind: "link"; text: string; url: string };

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\((?:https?:\/\/)[^)]+\))/g;

function parseInline(text: string): Segment[] {
  const segments: Segment[] = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    const index = match.index ?? 0;
    if (index > last) segments.push({ kind: "text", text: text.slice(last, index) });
    const token = match[0];
    if (token.startsWith("**")) {
      segments.push({ kind: "bold", text: token.slice(2, -2) });
    } else if (token.startsWith("`")) {
      segments.push({ kind: "code", text: token.slice(1, -1) });
    } else {
      const split = token.indexOf("](");
      segments.push({ kind: "link", text: token.slice(1, split), url: token.slice(split + 2, -1) });
    }
    last = index + token.length;
  }
  if (last < text.length) segments.push({ kind: "text", text: text.slice(last) });
  return segments;
}

function InlineText({ text, theme, style }: { text: string; theme: Theme; style?: object }) {
  return (
    <Text style={style}>
      {parseInline(text).map((segment, index) => {
        if (segment.kind === "bold") {
          return (
            <Text key={index} style={{ fontWeight: "700", color: theme.ink }}>
              {segment.text}
            </Text>
          );
        }
        if (segment.kind === "code") {
          return (
            <Text
              key={index}
              style={{ fontFamily: "Menlo", fontSize: 13, color: theme.ink, backgroundColor: theme.inset }}
            >
              {segment.text}
            </Text>
          );
        }
        if (segment.kind === "link") {
          return (
            <Text
              key={index}
              style={{ color: theme.acc, textDecorationLine: "underline" }}
              onPress={() => Linking.openURL(segment.url).catch(() => undefined)}
            >
              {segment.text}
            </Text>
          );
        }
        return <Text key={index}>{segment.text}</Text>;
      })}
    </Text>
  );
}

export function Markdown({ text, theme }: { text: string; theme: Theme }) {
  const styles = makeStyles(theme);
  const blocks: React.ReactNode[] = [];
  const lines = (text || "").replace(/\r\n/g, "\n").split("\n");

  lines.forEach((raw, index) => {
    const line = raw.trimEnd();
    if (!line.trim()) return; // spacing comes from block margins
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      blocks.push(
        <InlineText
          key={index}
          text={heading[2]}
          theme={theme}
          style={level === 1 ? styles.h1 : level === 2 ? styles.h2 : styles.h3}
        />,
      );
      return;
    }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) {
      blocks.push(
        <View key={index} style={styles.bulletRow}>
          <Text style={styles.bulletDot}>•</Text>
          <InlineText text={bullet[1]} theme={theme} style={styles.bulletText} />
        </View>,
      );
      return;
    }
    const numbered = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    if (numbered) {
      blocks.push(
        <View key={index} style={styles.bulletRow}>
          <Text style={styles.bulletDot}>{numbered[1]}.</Text>
          <InlineText text={numbered[2]} theme={theme} style={styles.bulletText} />
        </View>,
      );
      return;
    }
    blocks.push(<InlineText key={index} text={line} theme={theme} style={styles.paragraph} />);
  });

  return <View>{blocks}</View>;
}

function makeStyles(theme: Theme) {
  return StyleSheet.create({
    h1: { color: theme.ink, fontSize: 20, fontWeight: "800", marginTop: 6, marginBottom: 10, letterSpacing: -0.3 },
    h2: { color: theme.ink, fontSize: 16.5, fontWeight: "700", marginTop: 16, marginBottom: 6 },
    h3: { color: theme.ink, fontSize: 15, fontWeight: "700", marginTop: 12, marginBottom: 4 },
    paragraph: { color: theme.sec, fontSize: 15, lineHeight: 22, marginBottom: 8 },
    bulletRow: { flexDirection: "row", gap: 8, marginBottom: 6, paddingRight: 8 },
    bulletDot: { color: theme.mut, fontSize: 15, lineHeight: 22 },
    bulletText: { color: theme.sec, fontSize: 15, lineHeight: 22, flex: 1 },
  });
}
