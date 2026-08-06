/**
 * Position detail — price chart, stats grid, edit/delete.
 * Chart data comes from /quote/{symbol}/history (served by the price cache
 * when providers are rate-limited, so it works offline-ish too).
 */

import { useCallback, useEffect, useState } from "react";
import { ActivityIndicator, Alert, Pressable, ScrollView, StyleSheet, Text, View, useWindowDimensions } from "react-native";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";

import { serin, type Position, type SymbolHistory } from "../../src/api";
import { AreaChart } from "../../src/Sparkline";
import { money, signedMoney, signedPct, useTheme } from "../../src/theme";

const PERIODS = ["1m", "3m", "6m", "1y"] as const;

export default function PositionDetail() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const theme = useTheme();
  const { width } = useWindowDimensions();
  const [position, setPosition] = useState<Position | null>(null);
  const [series, setSeries] = useState<SymbolHistory | null>(null);
  const [period, setPeriod] = useState<(typeof PERIODS)[number]>("3m");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const positions = await serin.positions();
      const match = positions.find((p) => String(p.id) === String(id)) || null;
      setPosition(match);
      if (match && match.asset_type !== "cash") {
        const history = await serin.symbolHistory(match.symbol, period, match.asset_type);
        setSeries(history);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [id, period]);

  useEffect(() => {
    load();
  }, [load]);

  const styles = makeStyles(theme);

  if (error) {
    return (
      <View style={[styles.container, styles.center]}>
        <Text style={styles.body}>{error}</Text>
      </View>
    );
  }
  if (!position) {
    return (
      <View style={[styles.container, styles.center]}>
        <ActivityIndicator color={theme.up} />
      </View>
    );
  }

  const posGain = position.unrealized_gain >= 0;
  const closes = series?.closes || [];

  function confirmDelete() {
    Alert.alert("Delete position", `Remove ${position!.symbol} from your portfolio?`, [
      { text: "Cancel", style: "cancel" },
      {
        text: "Delete",
        style: "destructive",
        onPress: async () => {
          try {
            await serin.deletePosition(position!.id);
            router.back();
          } catch (err) {
            Alert.alert("Delete failed", err instanceof Error ? err.message : String(err));
          }
        },
      },
    ]);
  }

  return (
    <ScrollView style={styles.container} contentContainerStyle={{ paddingBottom: 40 }}>
      <Stack.Screen options={{ title: position.symbol }} />
      <View style={styles.header}>
        <Text style={styles.name}>{position.name || position.symbol}</Text>
        <Text style={styles.price}>{money(position.current_price, position.currency)}</Text>
        <Text style={[styles.gain, posGain ? { color: theme.up } : { color: theme.down }]}>
          {signedMoney(position.unrealized_gain)} ({signedPct(position.unrealized_gain_pct)})
        </Text>
      </View>

      <View style={styles.periodRow}>
        {PERIODS.map((option) => (
          <Pressable
            key={option}
            accessibilityRole="button"
            style={[styles.periodChip, period === option && styles.periodChipActive]}
            onPress={() => setPeriod(option)}
          >
            <Text style={[styles.periodText, period === option && styles.periodTextActive]}>
              {option.toUpperCase()}
            </Text>
          </Pressable>
        ))}
      </View>

      {closes.length >= 2 ? (
        <AreaChart values={closes} width={width - 40} />
      ) : (
        <Text style={[styles.body, { paddingHorizontal: 20 }]}>No price history yet — pull to refresh on the dashboard.</Text>
      )}

      <View style={styles.grid}>
        <Stat label="Quantity" value={String(position.quantity)} theme={theme} />
        <Stat label="Avg cost" value={money(position.average_cost, position.currency)} theme={theme} />
        <Stat label="Market value" value={money(position.market_value)} theme={theme} />
        <Stat label="Cost basis" value={money(position.total_cost)} theme={theme} />
        <Stat label="Broker" value={position.broker} theme={theme} />
        <Stat label="Sector" value={position.sector || "—"} theme={theme} />
      </View>

      <View style={styles.actions}>
        <Pressable
          accessibilityRole="button"
          style={styles.secondary}
          onPress={() => router.push({ pathname: "/add-position", params: { id: String(position.id) } })}
        >
          <Text style={styles.secondaryText}>Edit</Text>
        </Pressable>
        <Pressable accessibilityRole="button" style={styles.danger} onPress={confirmDelete}>
          <Text style={styles.dangerText}>Delete</Text>
        </Pressable>
      </View>
    </ScrollView>
  );
}

function Stat({ label, value, theme }: { label: string; value: string; theme: ReturnType<typeof useTheme> }) {
  return (
    <View style={{ width: "50%", paddingVertical: 10 }}>
      <Text style={{ color: theme.mut, fontSize: 12, textTransform: "uppercase", letterSpacing: 0.6 }}>{label}</Text>
      <Text style={{ color: theme.ink, fontSize: 16, fontWeight: "600", marginTop: 3 }}>{value}</Text>
    </View>
  );
}

function makeStyles(theme: ReturnType<typeof useTheme>) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.bg },
    center: { justifyContent: "center", alignItems: "center", padding: 24 },
    header: { padding: 20 },
    name: { color: theme.sec, fontSize: 15 },
    price: { color: theme.ink, fontSize: 34, fontWeight: "700", marginTop: 4 },
    gain: { fontSize: 15, marginTop: 4 },
    periodRow: { flexDirection: "row", gap: 8, paddingHorizontal: 20, marginBottom: 14 },
    periodChip: { paddingHorizontal: 12, paddingVertical: 6, borderRadius: 999, backgroundColor: theme.inset },
    periodChipActive: { backgroundColor: theme.up },
    periodText: { color: theme.sec, fontSize: 13, fontWeight: "600" },
    periodTextActive: { color: theme.bg },
    grid: { flexDirection: "row", flexWrap: "wrap", paddingHorizontal: 20, marginTop: 14 },
    actions: { flexDirection: "row", gap: 12, paddingHorizontal: 20, marginTop: 20 },
    secondary: { flex: 1, alignItems: "center", padding: 13, borderRadius: 10, backgroundColor: theme.inset },
    secondaryText: { color: theme.ink, fontWeight: "600" },
    danger: { flex: 1, alignItems: "center", padding: 13, borderRadius: 10, backgroundColor: "rgba(224,69,58,0.15)" },
    dangerText: { color: theme.down, fontWeight: "600" },
    body: { color: theme.sec, fontSize: 15, lineHeight: 22 },
  });
}
