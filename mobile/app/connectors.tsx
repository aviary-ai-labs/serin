/**
 * Connectors — read-only status view of the connector platform. Full
 * configuration stays on the web portal (keyboard + secrets belong there);
 * this screen answers "what's wired up?" at a glance.
 */

import { useEffect, useState } from "react";
import { ActivityIndicator, ScrollView, StyleSheet, Text, View } from "react-native";
import { Stack } from "expo-router";

import { serin, type ConnectorCard } from "../src/api";
import { useTheme } from "../src/theme";

const KIND_LABEL: Record<string, string> = {
  market_data: "Market data",
  holdings: "Holdings",
  insight: "Insights",
};

export default function Connectors() {
  const theme = useTheme();
  const [cards, setCards] = useState<ConnectorCard[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    serin
      .connectors()
      .then((data) => setCards(data.connectors || []))
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  const styles = makeStyles(theme);

  if (error) {
    return (
      <View style={[styles.container, styles.center]}>
        <Text style={styles.body}>{error}</Text>
      </View>
    );
  }
  if (!cards) {
    return (
      <View style={[styles.container, styles.center]}>
        <ActivityIndicator color={theme.up} />
      </View>
    );
  }

  const kinds = ["market_data", "holdings", "insight"];

  return (
    <ScrollView style={styles.container} contentContainerStyle={{ padding: 20, paddingBottom: 40 }}>
      <Stack.Screen options={{ title: "Connectors" }} />
      <Text style={styles.body}>
        Status view — configure connectors (keys, toggles) from the web app.
      </Text>
      {kinds.map((kind) => {
        const list = cards.filter((card) => card.manifest.kind === kind);
        if (!list.length) return null;
        return (
          <View key={kind} style={{ marginTop: 18 }}>
            <Text style={styles.kind}>{KIND_LABEL[kind] || kind}</Text>
            {list.map((card) => (
              <View key={card.manifest.id} style={styles.card}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.name}>{card.manifest.name}</Text>
                  <Text style={styles.desc} numberOfLines={2}>
                    {card.manifest.description}
                  </Text>
                </View>
                <View
                  style={[styles.badge, card.enabled ? styles.badgeOn : styles.badgeOff]}
                  accessibilityLabel={card.enabled ? "enabled" : "disabled"}
                >
                  <Text style={[styles.badgeText, card.enabled ? { color: theme.up } : { color: theme.mut }]}>
                    {card.enabled ? (card.configured ? "Active" : "On") : "Off"}
                  </Text>
                </View>
              </View>
            ))}
          </View>
        );
      })}
    </ScrollView>
  );
}

function makeStyles(theme: ReturnType<typeof useTheme>) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.bg },
    center: { justifyContent: "center", alignItems: "center", padding: 24 },
    body: { color: theme.sec, fontSize: 14, lineHeight: 20 },
    kind: { color: theme.mut, fontSize: 12, textTransform: "uppercase", letterSpacing: 0.8, marginBottom: 8 },
    card: {
      flexDirection: "row",
      alignItems: "center",
      gap: 12,
      backgroundColor: theme.card,
      borderColor: theme.border,
      borderWidth: StyleSheet.hairlineWidth,
      borderRadius: 12,
      padding: 14,
      marginBottom: 8,
    },
    name: { color: theme.ink, fontSize: 15, fontWeight: "600" },
    desc: { color: theme.mut, fontSize: 12.5, marginTop: 3, lineHeight: 17 },
    badge: { paddingHorizontal: 10, paddingVertical: 5, borderRadius: 999 },
    badgeOn: { backgroundColor: "rgba(79,201,138,0.15)" },
    badgeOff: { backgroundColor: "rgba(123,140,132,0.15)" },
    badgeText: { fontSize: 12, fontWeight: "700" },
  });
}
