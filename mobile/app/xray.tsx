/**
 * Portfolio X-ray — renders whatever POST /connectors/xray/run returns, same
 * contract as the web tab: upsell when the Intelligence pack is unlicensed,
 * full report when entitled. The app holds no paid logic. The dashboard only
 * links here when the connector exists, so a plain open-source backend never
 * shows this screen at all.
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
import { getBackendUrl, serin, type XrayReport } from "../src/api";
import { useTheme } from "../src/theme";

const pct = (x?: number | null) => `${(Math.max(0, x || 0) * 100).toFixed(1)}%`;
const pctFine = (x?: number | null) => `${(Math.max(0, x || 0) * 100).toFixed(2)}%`;
const signed = (x: number) => `${x >= 0 ? "+" : ""}${x.toFixed(1)}%`;
const PERIOD_LABELS: Record<string, string> = { "1m": "1M", "3m": "3M", ytd: "YTD", "1y": "1Y" };
const SIZE_LABELS: Record<string, string> = {
  mega: "Mega cap",
  large: "Large cap",
  mid: "Mid cap",
  small: "Small cap",
};

export default function Xray() {
  const theme = useTheme();
  const styles = makeStyles(theme);
  const [report, setReport] = useState<XrayReport | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      setReport(await serin.xray());
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

  if (error || !report) {
    return (
      <View style={[styles.container, styles.center]}>
        <Text style={styles.body}>{error || "X-ray unavailable."}</Text>
      </View>
    );
  }

  if (!report.entitled) {
    return (
      <View style={[styles.container, styles.center]}>
        <Text style={styles.badge}>INTELLIGENCE</Text>
        <Text style={styles.headline}>See what your portfolio is really made of</Text>
        <Text style={styles.body}>
          {report.message || "Add a license key to unlock the full X-ray report."}
        </Text>
        <Pressable
          accessibilityRole="button"
          style={styles.primary}
          onPress={async () => {
            const base = await getBackendUrl();
            if (base) Linking.openURL(`${base}/pricing`).catch(() => undefined);
          }}
        >
          <Text style={styles.primaryText}>See plans</Text>
        </Pressable>
      </View>
    );
  }

  const flags = report.flags || [];
  const largest = report.largest || [];
  const peak = largest[0]?.weight || 1;
  const fees = report.fee_drag;
  const factor = report.factor_snapshot;
  const overlap = report.cross_broker_overlap || [];
  const hhi = report.concentration_hhi || 0;
  const concentration = hhi > 0.25 ? "Concentrated" : hhi > 0.15 ? "Moderate" : "Diversified";

  const benchmark = report.benchmark;
  const benchHead =
    benchmark?.periods?.find(p => p.period === "3m") || benchmark?.periods?.[0];

  const stats: { label: string; value: string; sub?: string; tone?: "up" | "down" }[] = [
    {
      label: "Concentration",
      value: concentration,
      sub:
        report.effective_holdings != null
          ? `${report.effective_holdings} effective of ${report.holdings_count}`
          : undefined,
    },
    { label: "Top 5 weight", value: pct(report.top5_weight) },
    { label: "Cash drag", value: pct(report.cash_drag) },
  ];
  if (fees) {
    stats.push({
      label: "Fund fees",
      value: pctFine(fees.weighted_expense_ratio),
      sub: fees.funds?.length ? `≈ $${Math.round(fees.annual_cost)}/yr` : "none detected",
    });
  }
  if (factor?.weighted_beta != null) {
    stats.push({ label: "Beta", value: factor.weighted_beta.toFixed(2), sub: "vs market" });
  }
  if (factor?.weighted_dividend_yield != null) {
    stats.push({ label: "Div yield", value: pct(factor.weighted_dividend_yield), sub: "trailing" });
  }
  if (benchmark && benchHead) {
    stats.push({
      label: `vs ${benchmark.symbol} · ${PERIOD_LABELS[benchHead.period] || benchHead.period}`,
      value: signed(benchHead.delta_pct),
      sub: `you ${signed(benchHead.portfolio_pct)} · ${signed(benchHead.benchmark_pct)}`,
      tone: benchHead.delta_pct >= 0 ? "up" : "down",
    });
  }

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
      {flags.map((flag, index) => (
        <View key={index} style={styles.flag}>
          <Text style={styles.flagText}>{flag}</Text>
        </View>
      ))}

      <View style={styles.statGrid}>
        {stats.map(stat => (
          <View key={stat.label} style={styles.statCard}>
            <Text style={styles.statLabel}>{stat.label}</Text>
            <Text
              style={[
                styles.statValue,
                stat.tone === "up" && { color: theme.up },
                stat.tone === "down" && { color: theme.down },
              ]}
            >
              {stat.value}
            </Text>
            {!!stat.sub && <Text style={styles.statSub}>{stat.sub}</Text>}
          </View>
        ))}
      </View>

      {benchmark && (
        <MixCard title={`Vs ${benchmark.symbol === "SPY" ? "S&P 500" : benchmark.symbol}`} theme={theme}>
          {benchmark.periods.map(row => (
            <View key={row.period} style={styles.benchRow}>
              <Text style={styles.benchPeriod}>{PERIOD_LABELS[row.period] || row.period}</Text>
              <Text style={styles.benchCell}>{signed(row.portfolio_pct)}</Text>
              <Text style={styles.benchCell}>{signed(row.benchmark_pct)}</Text>
              <Text
                style={[styles.benchCell, { color: row.delta_pct >= 0 ? theme.up : theme.down }]}
              >
                {signed(row.delta_pct)}
              </Text>
            </View>
          ))}
          <Text style={styles.foot}>
            You · {benchmark.symbol} · Δ — holdings-based, deposits ignored. Coverage{" "}
            {pct(benchmark.periods[benchmark.periods.length - 1]?.coverage)}.
          </Text>
        </MixCard>
      )}

      {largest.length > 0 && (
        <MixCard title="Largest positions" theme={theme}>
          {largest.map(item => (
            <BarRow
              key={item.symbol}
              label={item.symbol}
              fraction={item.weight / peak}
              value={pct(item.weight)}
              theme={theme}
            />
          ))}
        </MixCard>
      )}

      {factor && (
        <MixCard title="Factor snapshot" theme={theme}>
          {Object.entries(factor.size_mix || {}).map(([bucket, weight]) => (
            <BarRow
              key={bucket}
              label={SIZE_LABELS[bucket] || bucket}
              fraction={weight / Math.max(...Object.values(factor.size_mix || { x: 1 }))}
              value={pct(weight)}
              theme={theme}
            />
          ))}
          <Text style={styles.foot}>Based on {pct(factor.coverage)} of invested value</Text>
        </MixCard>
      )}

      <Mix title="Sector mix" mix={report.sector_mix} theme={theme} />
      <Mix title="Asset mix" mix={report.asset_mix} theme={theme} capitalize />
      <Mix title="By account" mix={report.broker_mix} theme={theme} capitalize />
      <Mix title="By currency" mix={report.currency_mix} theme={theme} />

      {overlap.length > 0 && (
        <MixCard title="Held across accounts" theme={theme}>
          {overlap.map(item => (
            <View key={item.symbol} style={styles.overlapRow}>
              <Text style={styles.overlapSymbol}>{item.symbol}</Text>
              <Text style={styles.overlapBrokers}>{item.brokers.join(" · ")}</Text>
            </View>
          ))}
          <Text style={styles.foot}>Same symbol in more than one account — check for doubling-up.</Text>
        </MixCard>
      )}
    </ScrollView>
  );
}

function Mix({
  title,
  mix,
  theme,
  capitalize,
}: {
  title: string;
  mix?: Record<string, number>;
  theme: ReturnType<typeof useTheme>;
  capitalize?: boolean;
}) {
  const entries = Object.entries(mix || {}).filter(([, weight]) => weight >= 0.0005);
  if (entries.length < 2) return null; // a single-slice mix says nothing
  const peak = Math.max(...entries.map(([, weight]) => weight));
  return (
    <MixCard title={title} theme={theme}>
      {entries.map(([name, weight]) => (
        <BarRow
          key={name}
          label={capitalize ? name.charAt(0).toUpperCase() + name.slice(1) : name}
          fraction={weight / peak}
          value={pct(weight)}
          theme={theme}
        />
      ))}
    </MixCard>
  );
}

function MixCard({
  title,
  theme,
  children,
}: {
  title: string;
  theme: ReturnType<typeof useTheme>;
  children: React.ReactNode;
}) {
  const styles = makeStyles(theme);
  return (
    <View style={styles.card}>
      <Text style={styles.cardTitle}>{title}</Text>
      {children}
    </View>
  );
}

function BarRow({
  label,
  fraction,
  value,
  theme,
}: {
  label: string;
  fraction: number;
  value: string;
  theme: ReturnType<typeof useTheme>;
}) {
  const styles = makeStyles(theme);
  return (
    <View style={styles.barRow}>
      <Text style={styles.barLabel} numberOfLines={1}>
        {label}
      </Text>
      <View style={styles.barTrack}>
        <View style={[styles.barFill, { width: `${Math.min(100, Math.max(2, fraction * 100))}%` }]} />
      </View>
      <Text style={styles.barValue}>{value}</Text>
    </View>
  );
}

function makeStyles(theme: ReturnType<typeof useTheme>) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.bg },
    center: { justifyContent: "center", alignItems: "center", padding: 24, gap: 10 },
    body: { color: theme.sec, fontSize: 15, textAlign: "center", lineHeight: 22 },
    badge: { color: theme.acc, fontSize: 11, fontWeight: "800", letterSpacing: 1 },
    headline: { color: theme.ink, fontSize: 20, fontWeight: "800", textAlign: "center" },
    primary: {
      backgroundColor: theme.up,
      paddingHorizontal: 22,
      paddingVertical: 12,
      borderRadius: 10,
      marginTop: 8,
    },
    primaryText: { color: theme.bg, fontWeight: "700", fontSize: 15 },
    flag: {
      backgroundColor: theme.inset,
      borderLeftColor: theme.down,
      borderLeftWidth: 3,
      borderRadius: 10,
      padding: 12,
      marginBottom: 10,
    },
    flagText: { color: theme.down, fontSize: 13.5, fontWeight: "600", lineHeight: 19 },
    statGrid: { flexDirection: "row", flexWrap: "wrap", gap: 10, marginTop: 6, marginBottom: 16 },
    statCard: {
      backgroundColor: theme.card,
      borderColor: theme.border,
      borderWidth: StyleSheet.hairlineWidth,
      borderRadius: 12,
      padding: 12,
      minWidth: "30%",
      flexGrow: 1,
    },
    statLabel: { color: theme.mut, fontSize: 11.5, fontWeight: "600", marginBottom: 4 },
    statValue: { color: theme.ink, fontSize: 18, fontWeight: "800", letterSpacing: -0.3 },
    statSub: { color: theme.mut, fontSize: 11.5, marginTop: 2 },
    card: {
      backgroundColor: theme.card,
      borderColor: theme.border,
      borderWidth: StyleSheet.hairlineWidth,
      borderRadius: 14,
      padding: 16,
      marginBottom: 12,
      gap: 9,
    },
    cardTitle: { color: theme.ink, fontSize: 15, fontWeight: "700", marginBottom: 2 },
    barRow: { flexDirection: "row", alignItems: "center", gap: 10 },
    barLabel: { color: theme.sec, fontSize: 13, fontWeight: "600", minWidth: 92, maxWidth: 130 },
    barTrack: {
      flex: 1,
      height: 7,
      borderRadius: 999,
      backgroundColor: theme.inset,
      overflow: "hidden",
    },
    barFill: { height: "100%", borderRadius: 999, backgroundColor: theme.acc },
    barValue: { color: theme.sec, fontSize: 12.5, fontWeight: "600", minWidth: 46, textAlign: "right" },
    overlapRow: { flexDirection: "row", justifyContent: "space-between", gap: 10 },
    benchRow: { flexDirection: "row", alignItems: "center", gap: 8 },
    benchPeriod: { color: theme.ink, fontSize: 13, fontWeight: "700", width: 40 },
    benchCell: { color: theme.sec, fontSize: 13, fontWeight: "600", flex: 1, textAlign: "right" },
    overlapSymbol: { color: theme.ink, fontSize: 14, fontWeight: "700" },
    overlapBrokers: { color: theme.sec, fontSize: 13 },
    foot: { color: theme.mut, fontSize: 11.5, marginTop: 2 },
  });
}
