/**
 * Add / edit position — presented modally. With an `id` param it loads the
 * existing position for editing; without, it creates a new one.
 */

import { useEffect, useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";

import { serin, type PositionInput } from "../src/api";
import { useTheme } from "../src/theme";

const ASSET_TYPES = ["stock", "etf", "crypto", "cash", "option"] as const;

export default function AddPosition() {
  const { id } = useLocalSearchParams<{ id?: string }>();
  const router = useRouter();
  const theme = useTheme();
  const editing = Boolean(id);

  const [form, setForm] = useState<PositionInput>({
    symbol: "",
    name: "",
    broker: "manual",
    asset_type: "stock",
    quantity: 0,
    average_cost: 0,
    current_price: 0,
    currency: "USD",
  });
  const [quantityText, setQuantityText] = useState("");
  const [costText, setCostText] = useState("");
  const [priceText, setPriceText] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!id) return;
    serin
      .positions()
      .then((positions) => {
        const match = positions.find((p) => String(p.id) === String(id));
        if (!match) return;
        setForm({
          symbol: match.symbol,
          name: match.name === match.symbol ? "" : match.name,
          broker: match.broker,
          asset_type: match.asset_type,
          quantity: match.quantity,
          average_cost: match.average_cost,
          current_price: match.current_price,
          currency: match.currency || "USD",
        });
        setQuantityText(String(match.quantity));
        setCostText(String(match.average_cost));
        setPriceText(String(match.current_price));
      })
      .catch(() => null);
  }, [id]);

  async function submit() {
    const body: PositionInput = {
      ...form,
      symbol: form.symbol.trim().toUpperCase(),
      quantity: parseFloat(quantityText) || 0,
      average_cost: parseFloat(costText) || 0,
      current_price: parseFloat(priceText) || 0,
    };
    if (!body.symbol) {
      Alert.alert("Symbol required");
      return;
    }
    setBusy(true);
    try {
      if (editing) await serin.updatePosition(Number(id), body);
      else await serin.createPosition(body);
      router.back();
    } catch (err) {
      Alert.alert("Save failed", err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const styles = makeStyles(theme);

  return (
    <ScrollView style={styles.container} contentContainerStyle={{ padding: 20, paddingBottom: 48 }}>
      <Stack.Screen options={{ title: editing ? `Edit ${form.symbol}` : "Add position" }} />

      <Field label="Symbol" theme={theme}>
        <TextInput
          style={styles.input}
          value={form.symbol}
          autoCapitalize="characters"
          autoCorrect={false}
          placeholder="AAPL"
          placeholderTextColor={theme.mut}
          onChangeText={(symbol) => setForm((prev) => ({ ...prev, symbol }))}
          accessibilityLabel="Symbol"
        />
      </Field>

      <Field label="Type" theme={theme}>
        <View style={styles.segmentRow}>
          {ASSET_TYPES.map((type) => (
            <Pressable
              key={type}
              accessibilityRole="button"
              style={[styles.segment, form.asset_type === type && styles.segmentActive]}
              onPress={() => setForm((prev) => ({ ...prev, asset_type: type }))}
            >
              <Text style={[styles.segmentText, form.asset_type === type && styles.segmentTextActive]}>{type}</Text>
            </Pressable>
          ))}
        </View>
      </Field>

      <Field label="Broker" theme={theme}>
        <TextInput
          style={styles.input}
          value={form.broker}
          autoCapitalize="none"
          placeholder="robinhood, etrade…"
          placeholderTextColor={theme.mut}
          onChangeText={(broker) => setForm((prev) => ({ ...prev, broker }))}
          accessibilityLabel="Broker"
        />
      </Field>

      <Field label={form.asset_type === "cash" ? "Balance" : "Quantity"} theme={theme}>
        <TextInput
          style={styles.input}
          value={quantityText}
          keyboardType="decimal-pad"
          placeholder="0"
          placeholderTextColor={theme.mut}
          onChangeText={setQuantityText}
          accessibilityLabel="Quantity"
        />
      </Field>

      {form.asset_type !== "cash" && (
        <>
          <Field label="Average cost" theme={theme}>
            <TextInput
              style={styles.input}
              value={costText}
              keyboardType="decimal-pad"
              placeholder="0.00"
              placeholderTextColor={theme.mut}
              onChangeText={setCostText}
              accessibilityLabel="Average cost"
            />
          </Field>
          <Field label="Current price (refresh later for live quotes)" theme={theme}>
            <TextInput
              style={styles.input}
              value={priceText}
              keyboardType="decimal-pad"
              placeholder="0.00"
              placeholderTextColor={theme.mut}
              onChangeText={setPriceText}
              accessibilityLabel="Current price"
            />
          </Field>
        </>
      )}

      <Pressable accessibilityRole="button" style={[styles.primary, busy && { opacity: 0.6 }]} disabled={busy} onPress={submit}>
        <Text style={styles.primaryText}>{busy ? "Saving…" : editing ? "Save changes" : "Add position"}</Text>
      </Pressable>
    </ScrollView>
  );
}

function Field({ label, children, theme }: { label: string; children: React.ReactNode; theme: ReturnType<typeof useTheme> }) {
  return (
    <View style={{ marginBottom: 16 }}>
      <Text style={{ color: theme.mut, fontSize: 12, textTransform: "uppercase", letterSpacing: 0.6, marginBottom: 6 }}>
        {label}
      </Text>
      {children}
    </View>
  );
}

function makeStyles(theme: ReturnType<typeof useTheme>) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.bg },
    input: {
      backgroundColor: theme.inset,
      borderColor: theme.border,
      borderWidth: StyleSheet.hairlineWidth,
      borderRadius: 10,
      color: theme.ink,
      fontSize: 16,
      paddingHorizontal: 14,
      paddingVertical: 12,
    },
    segmentRow: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
    segment: { paddingHorizontal: 12, paddingVertical: 8, borderRadius: 999, backgroundColor: theme.inset },
    segmentActive: { backgroundColor: theme.up },
    segmentText: { color: theme.sec, fontSize: 14, fontWeight: "600" },
    segmentTextActive: { color: theme.bg },
    primary: { backgroundColor: theme.up, alignItems: "center", padding: 14, borderRadius: 10, marginTop: 8 },
    primaryText: { color: theme.bg, fontWeight: "700", fontSize: 16 },
  });
}
