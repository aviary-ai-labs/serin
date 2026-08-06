/**
 * News — the same GET /news feed as the web tab: portfolio-relevant headlines
 * first (matched against holdings), then broad market news. Tap opens the
 * story in the browser.
 */

import { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  Linking,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { serin, type NewsFeed, type NewsItem } from "../src/api";
import { useTheme } from "../src/theme";

function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  if (!then) return "";
  const minutes = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)}h`;
  return `${Math.round(minutes / (60 * 24))}d`;
}

export default function News() {
  const theme = useTheme();
  const styles = makeStyles(theme);
  const [feed, setFeed] = useState<NewsFeed | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      setFeed(await serin.news());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) {
    return (
      <View style={[styles.container, styles.center]}>
        <ActivityIndicator color={theme.up} />
      </View>
    );
  }

  const sections: { title: string; items: NewsItem[] }[] = [
    { title: "Your holdings", items: feed?.portfolio_news || [] },
    { title: "Markets", items: feed?.market_news || [] },
  ].filter(section => section.items.length > 0);

  return (
    <ScrollView
      style={styles.container}
      contentContainerStyle={{ padding: 20, paddingBottom: 40 }}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          tintColor={theme.up}
          onRefresh={() => {
            setRefreshing(true);
            load();
          }}
        />
      }
    >
      {error !== "" && <Text style={styles.body}>{error}</Text>}
      {sections.length === 0 && !error && <Text style={styles.body}>No headlines right now.</Text>}
      {sections.map(section => (
        <View key={section.title} style={{ marginBottom: 18 }}>
          <Text style={styles.sectionTitle}>{section.title}</Text>
          {section.items.map((item, index) => (
            <Pressable
              key={`${section.title}-${index}`}
              accessibilityRole="link"
              style={({ pressed }) => [styles.row, pressed && { opacity: 0.7 }]}
              onPress={() => item.url && Linking.openURL(item.url).catch(() => undefined)}
            >
              <Text style={styles.title}>{item.title}</Text>
              <Text style={styles.meta}>
                {item.source}
                {item.published ? ` · ${timeAgo(item.published)}` : ""}
                {item.tickers?.length ? ` · ${item.tickers.join(" ")}` : ""}
              </Text>
            </Pressable>
          ))}
        </View>
      ))}
    </ScrollView>
  );
}

function makeStyles(theme: ReturnType<typeof useTheme>) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.bg },
    center: { justifyContent: "center", alignItems: "center", padding: 24 },
    body: { color: theme.sec, fontSize: 15, textAlign: "center", marginTop: 24 },
    sectionTitle: {
      color: theme.mut,
      fontSize: 12,
      textTransform: "uppercase",
      letterSpacing: 0.8,
      marginBottom: 6,
    },
    row: {
      paddingVertical: 12,
      borderBottomColor: theme.border,
      borderBottomWidth: StyleSheet.hairlineWidth,
      gap: 4,
    },
    title: { color: theme.ink, fontSize: 15, fontWeight: "600", lineHeight: 21 },
    meta: { color: theme.mut, fontSize: 12.5 },
  });
}
