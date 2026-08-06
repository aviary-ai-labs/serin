/**
 * Dashboard — totals + day change + positions with sparklines.
 *
 * Offline-first: every good load snapshots to AsyncStorage; when the network
 * fails the snapshot renders with a "last synced" banner instead of an error
 * wall. Pull to refresh re-quotes prices server-side.
 */

import { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { Stack, useRouter, useFocusEffect } from "expo-router";

import {
  serin,
  getBackendUrl,
  loadSnapshot,
  saveSnapshot,
  type Portfolio,
  type Position,
  type PriceHistory,
} from "../src/api";
import { Sparkline } from "../src/Sparkline";
import { money, signedPct, useTheme } from "../src/theme";

export default function Dashboard() {
  const router = useRouter();
  const theme = useTheme();
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [history, setHistory] = useState<PriceHistory["history"]>({});
  const [error, setError] = useState("");
  const [offlineAt, setOfflineAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [needsConfig, setNeedsConfig] = useState(false);
  const [hasXray, setHasXray] = useState(false);

  const load = useCallback(async (withQuoteRefresh = false) => {
    const url = await getBackendUrl();
    if (!url) {
      setNeedsConfig(true);
      setLoading(false);
      return;
    }
    setNeedsConfig(false);
    setError("");
    try {
      if (withQuoteRefresh) await serin.refreshPrices().catch(() => null);
      const [pf, hist] = await Promise.all([serin.portfolio(), serin.priceHistory("3m")]);
      setPortfolio(pf);
      setHistory(hist.history || {});
      setOfflineAt(null);
      await saveSnapshot({ portfolio: pf, positions: pf.positions, history: hist.history || {} });
    } catch (err) {
      const cached = await loadSnapshot();
      if (cached) {
        setPortfolio(cached.portfolio);
        setHistory(cached.history || {});
        setOfflineAt(cached.savedAt);
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load();
    // Feature-detect the Intelligence pack: the X-ray entry only exists when
    // the connector is registered (mirrors the web — no pack, no trace).
    serin
      .connectors()
      .then(res => setHasXray((res.connectors || []).some(c => c.manifest.id === "xray")))
      .catch(() => setHasXray(false));
  }, [load]);

  useFocusEffect(
    useCallback(() => {
      // Re-load quietly whenever the screen regains focus (post add/import).
      load();
    }, [load]),
  );

  const styles = makeStyles(theme);

  if (loading) {
    return (
      <View style={[styles.container, styles.center]}>
        <ActivityIndicator color={theme.up} />
      </View>
    );
  }

  if (needsConfig) {
    return (
      <View style={[styles.container, styles.center]}>
        <Text style={styles.headline}>Welcome to Serin</Text>
        <Text style={styles.body}>Point this app at your Serin backend to get started — or scan the pairing QR from the web app's Connectors tab.</Text>
        <Pressable accessibilityRole="button" style={styles.primary} onPress={() => router.push("/settings")}>
          <Text style={styles.primaryText}>Configure backend</Text>
        </Pressable>
      </View>
    );
  }

  if (error || !portfolio) {
    return (
      <View style={[styles.container, styles.center]}>
        <Text style={styles.headline}>Couldn't reach Serin</Text>
        <Text style={styles.body}>{error || "Empty response from /api/v1/portfolio."}</Text>
        <Pressable accessibilityRole="button" style={styles.primary} onPress={() => load()}>
          <Text style={styles.primaryText}>Retry</Text>
        </Pressable>
        <Pressable accessibilityRole="button" style={styles.linkRow} onPress={() => router.push("/settings")}>
          <Text style={styles.linkText}>Settings →</Text>
        </Pressable>
      </View>
    );
  }

  const positive = portfolio.total_gain >= 0;

  // Header toolbar: refresh is a glanceable icon; "+" opens a labeled sheet
  // for the one-off entry paths (manual add / Smart Import).
  const headerRight = () => (
    <View style={{ flexDirection: "row", alignItems: "center", gap: 22, marginRight: 4 }}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel="Add position or import"
        hitSlop={12}
        onPress={() =>
          Alert.alert("Add holdings", undefined, [
            { text: "Add position manually", onPress: () => router.push("/add-position") },
            { text: "Smart Import (camera)", onPress: () => router.push("/smart-import") },
            { text: "Cancel", style: "cancel" },
          ])
        }
      >
        <Text style={{ color: theme.acc, fontSize: 26, fontWeight: "500", lineHeight: 28 }}>＋</Text>
      </Pressable>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel="Refresh prices"
        hitSlop={12}
        disabled={refreshing}
        onPress={() => {
          setRefreshing(true);
          load(true);
        }}
      >
        {refreshing ? (
          <ActivityIndicator size="small" color={theme.acc} />
        ) : (
          <Text style={{ color: theme.acc, fontSize: 22, fontWeight: "600" }}>↻</Text>
        )}
      </Pressable>
    </View>
  );

  return (
    <FlatList<Position>
      data={portfolio.positions.filter((p) => p.asset_type !== "cash")}
      keyExtractor={(item) => String(item.id)}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          tintColor={theme.up}
          onRefresh={() => {
            setRefreshing(true);
            load(true);
          }}
        />
      }
      ListHeaderComponent={
        <View style={styles.header}>
          <Stack.Screen options={{ headerRight }} />
          {offlineAt && (
            <View style={styles.offlineBanner} accessibilityRole="alert">
              <Text style={styles.offlineText}>
                Offline — showing data from {new Date(offlineAt).toLocaleString()}
              </Text>
            </View>
          )}
          <Text style={styles.label}>Total value</Text>
          <Text style={styles.totalValue} accessibilityRole="header">
            {money(portfolio.total_value)}
          </Text>
          <Text style={[styles.totalGain, positive ? styles.pos : styles.neg]}>
            {positive ? "+" : "−"}
            {money(Math.abs(portfolio.total_gain))} ({signedPct(portfolio.total_gain_pct)})
          </Text>
          <View style={styles.actionRow}>
            <Pressable accessibilityRole="button" style={styles.chip} onPress={() => router.push("/briefing")}>
              <Text style={styles.chipText}>Briefing</Text>
            </Pressable>
            <Pressable accessibilityRole="button" style={styles.chip} onPress={() => router.push("/news")}>
              <Text style={styles.chipText}>News</Text>
            </Pressable>
            {hasXray && (
              <Pressable accessibilityRole="button" style={styles.chip} onPress={() => router.push("/xray")}>
                <Text style={styles.chipText}>X-ray</Text>
              </Pressable>
            )}
            <Pressable accessibilityRole="button" style={styles.chip} onPress={() => router.push("/connectors")}>
              <Text style={styles.chipText}>Connectors</Text>
            </Pressable>
          </View>
          <Text style={[styles.label, { marginTop: 22 }]}>Positions</Text>
        </View>
      }
      renderItem={({ item }) => {
        const posGain = item.unrealized_gain >= 0;
        const closes = history[item.symbol]?.closes || [];
        return (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`${item.symbol}, ${money(item.market_value)}`}
            style={({ pressed }) => [styles.row, pressed && { opacity: 0.7 }]}
            onPress={() => router.push({ pathname: "/position/[id]", params: { id: String(item.id) } })}
          >
            <View style={{ flex: 1 }}>
              <Text style={styles.symbol}>{item.symbol}</Text>
              <Text style={styles.subtle}>
                {item.broker} · {item.quantity} sh
              </Text>
            </View>
            {closes.length >= 2 && <Sparkline values={closes.slice(-30)} />}
            <View style={{ alignItems: "flex-end", marginLeft: 12, minWidth: 92 }}>
              <Text style={styles.value}>{money(item.market_value)}</Text>
              <Text style={[styles.subtle, posGain ? styles.pos : styles.neg]}>
                {signedPct(item.unrealized_gain_pct)}
              </Text>
            </View>
          </Pressable>
        );
      }}
      contentContainerStyle={{ paddingBottom: 32 }}
      style={styles.container}
    />
  );
}

function makeStyles(theme: ReturnType<typeof useTheme>) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.bg },
    center: { justifyContent: "center", alignItems: "center", padding: 24 },
    header: { padding: 20, paddingBottom: 0 },
    offlineBanner: {
      backgroundColor: theme.inset,
      borderRadius: 10,
      padding: 10,
      marginBottom: 14,
      borderWidth: StyleSheet.hairlineWidth,
      borderColor: theme.border,
    },
    offlineText: { color: theme.sec, fontSize: 13 },
    label: { color: theme.mut, fontSize: 12, textTransform: "uppercase", letterSpacing: 0.8, marginBottom: 6 },
    totalValue: { color: theme.ink, fontSize: 38, fontWeight: "700", letterSpacing: -0.5 },
    totalGain: { fontSize: 16, marginTop: 4 },
    pos: { color: theme.up },
    neg: { color: theme.down },
    actionRow: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginTop: 16 },
    chip: {
      backgroundColor: theme.inset,
      borderColor: theme.border,
      borderWidth: StyleSheet.hairlineWidth,
      paddingHorizontal: 12,
      paddingVertical: 8,
      borderRadius: 999,
    },
    chipText: { color: theme.ink, fontSize: 14, fontWeight: "600" },
    linkRow: { marginTop: 14 },
    linkText: { color: theme.up, fontSize: 15 },
    row: {
      flexDirection: "row",
      alignItems: "center",
      paddingHorizontal: 20,
      paddingVertical: 14,
      borderBottomColor: theme.border,
      borderBottomWidth: StyleSheet.hairlineWidth,
    },
    symbol: { color: theme.ink, fontSize: 16, fontWeight: "600" },
    subtle: { color: theme.mut, fontSize: 13, marginTop: 2 },
    value: { color: theme.ink, fontSize: 16, fontWeight: "500" },
    headline: { color: theme.ink, fontSize: 22, fontWeight: "700", marginBottom: 8 },
    body: { color: theme.sec, fontSize: 15, textAlign: "center", marginBottom: 20, lineHeight: 22 },
    primary: { backgroundColor: theme.up, paddingHorizontal: 22, paddingVertical: 12, borderRadius: 10 },
    primaryText: { color: theme.bg, fontWeight: "600", fontSize: 15 },
  });
}
