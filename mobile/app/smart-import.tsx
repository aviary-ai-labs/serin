/**
 * Smart Import — the mobile killer feature. Snap a photo of a brokerage
 * screen (or pick a screenshot), the backend's AI extracts positions, you
 * review and confirm. Nothing is saved until Import is tapped — the same
 * mandatory-review contract as the web app.
 */

import { useState } from "react";
import { Alert, Image, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import { Stack, useRouter } from "expo-router";
import * as ImagePicker from "expo-image-picker";

import { serin, type ExtractResult, type ExtractRow } from "../src/api";
import { useTheme } from "../src/theme";

type Stage = "intake" | "extracting" | "review";

export default function SmartImport() {
  const router = useRouter();
  const theme = useTheme();
  const [stage, setStage] = useState<Stage>("intake");
  const [imageUri, setImageUri] = useState<string | null>(null);
  const [result, setResult] = useState<ExtractResult | null>(null);
  const [rows, setRows] = useState<ExtractRow[]>([]);
  const [busy, setBusy] = useState(false);

  async function pick(fromCamera: boolean) {
    const permission = fromCamera
      ? await ImagePicker.requestCameraPermissionsAsync()
      : await ImagePicker.requestMediaLibraryPermissionsAsync();
    if (!permission.granted) {
      Alert.alert("Permission needed", fromCamera ? "Allow camera access to snap your statement." : "Allow photo access to pick a screenshot.");
      return;
    }
    const picked = fromCamera
      ? await ImagePicker.launchCameraAsync({ quality: 0.8 })
      : await ImagePicker.launchImageLibraryAsync({ quality: 0.8 });
    if (picked.canceled || !picked.assets?.length) return;
    const asset = picked.assets[0];
    setImageUri(asset.uri);
    await extract(asset.uri, asset.mimeType || "image/jpeg");
  }

  async function extract(uri: string, mime: string) {
    setStage("extracting");
    try {
      const form = new FormData();
      // React Native FormData file part: {uri, name, type}.
      form.append("file", { uri, name: "statement.jpg", type: mime } as unknown as Blob);
      const extracted = await serin.importExtract(form);
      setResult(extracted);
      setRows(extracted.rows || []);
      setStage("review");
    } catch (err) {
      Alert.alert("Extraction failed", err instanceof Error ? err.message : String(err));
      setStage("intake");
    }
  }

  function updateRow(index: number, key: keyof ExtractRow, value: string) {
    setRows((prev) =>
      prev.map((row, i) => {
        if (i !== index) return row;
        if (key === "quantity" || key === "average_cost") {
          return { ...row, [key]: parseFloat(value) || 0 };
        }
        return { ...row, [key]: key === "symbol" ? value.toUpperCase() : value };
      }),
    );
  }

  async function runImport() {
    const importable = rows.filter((row) => row.symbol && row.symbol.trim());
    if (!importable.length) {
      Alert.alert("Nothing to import", "Every row needs at least a symbol.");
      return;
    }
    setBusy(true);
    try {
      const outcome = await serin.bulkInsert(importable, false);
      Alert.alert("Imported", `${outcome.inserted} position${outcome.inserted === 1 ? "" : "s"} added${outcome.skipped ? ` · ${outcome.skipped} skipped` : ""}.`);
      router.back();
    } catch (err) {
      Alert.alert("Import failed", err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const styles = makeStyles(theme);

  return (
    <ScrollView style={styles.container} contentContainerStyle={{ padding: 20, paddingBottom: 48 }}>
      <Stack.Screen options={{ title: "Smart Import" }} />

      {stage === "intake" && (
        <View>
          <Text style={styles.blurb}>
            Snap your brokerage screen or pick a screenshot. AI extracts the positions; you review
            every row before anything is saved.
          </Text>
          <Pressable accessibilityRole="button" style={styles.primary} onPress={() => pick(true)}>
            <Text style={styles.primaryText}>📷 Take photo</Text>
          </Pressable>
          <Pressable accessibilityRole="button" style={styles.secondary} onPress={() => pick(false)}>
            <Text style={styles.secondaryText}>Choose from library</Text>
          </Pressable>
          <Text style={styles.notice}>
            🔒 The image is sent to your configured AI provider for parsing. Crop or redact account
            numbers before uploading.
          </Text>
        </View>
      )}

      {stage === "extracting" && (
        <View style={{ alignItems: "center", paddingTop: 40 }}>
          {imageUri && <Image source={{ uri: imageUri }} style={styles.preview} accessibilityLabel="statement preview" />}
          <Text style={[styles.blurb, { marginTop: 18 }]}>Extracting positions…</Text>
        </View>
      )}

      {stage === "review" && result && (
        <View>
          <Text style={styles.blurb}>
            Found {rows.length} position{rows.length === 1 ? "" : "s"} · {result.model} · ~$
            {(result.cost_usd || 0).toFixed(4)}
          </Text>
          {rows.map((row, index) => (
            <View key={index} style={styles.rowCard}>
              <View style={styles.rowLine}>
                <TextInput
                  style={[styles.cell, styles.cellSymbol]}
                  value={row.symbol}
                  autoCapitalize="characters"
                  placeholder="SYM"
                  placeholderTextColor={theme.mut}
                  onChangeText={(value) => updateRow(index, "symbol", value)}
                  accessibilityLabel={`Row ${index + 1} symbol`}
                />
                <TextInput
                  style={[styles.cell, { flex: 1 }]}
                  value={row.broker}
                  autoCapitalize="none"
                  placeholder="broker"
                  placeholderTextColor={theme.mut}
                  onChangeText={(value) => updateRow(index, "broker", value)}
                  accessibilityLabel={`Row ${index + 1} broker`}
                />
              </View>
              <View style={styles.rowLine}>
                <TextInput
                  style={[styles.cell, { flex: 1 }]}
                  value={String(row.quantity ?? "")}
                  keyboardType="decimal-pad"
                  placeholder="qty"
                  placeholderTextColor={theme.mut}
                  onChangeText={(value) => updateRow(index, "quantity", value)}
                  accessibilityLabel={`Row ${index + 1} quantity`}
                />
                <TextInput
                  style={[styles.cell, { flex: 1 }]}
                  value={String(row.average_cost ?? "")}
                  keyboardType="decimal-pad"
                  placeholder="avg cost"
                  placeholderTextColor={theme.mut}
                  onChangeText={(value) => updateRow(index, "average_cost", value)}
                  accessibilityLabel={`Row ${index + 1} average cost`}
                />
              </View>
              {(row.warnings?.length ?? 0) > 0 && (
                <Text style={styles.warning}>{row.warnings!.join(" · ")}</Text>
              )}
            </View>
          ))}
          <Pressable accessibilityRole="button" style={[styles.primary, busy && { opacity: 0.6 }]} disabled={busy} onPress={runImport}>
            <Text style={styles.primaryText}>{busy ? "Importing…" : `Import ${rows.filter((r) => r.symbol).length}`}</Text>
          </Pressable>
          <Pressable accessibilityRole="button" style={styles.secondary} onPress={() => setStage("intake")}>
            <Text style={styles.secondaryText}>Start over</Text>
          </Pressable>
        </View>
      )}
    </ScrollView>
  );
}

function makeStyles(theme: ReturnType<typeof useTheme>) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.bg },
    blurb: { color: theme.sec, fontSize: 15, lineHeight: 22, marginBottom: 18 },
    notice: {
      color: theme.sec,
      fontSize: 13,
      lineHeight: 19,
      backgroundColor: theme.inset,
      borderRadius: 10,
      padding: 12,
      marginTop: 18,
    },
    preview: { width: 220, height: 160, borderRadius: 12, opacity: 0.85 },
    primary: { backgroundColor: theme.up, alignItems: "center", padding: 14, borderRadius: 10, marginTop: 6 },
    primaryText: { color: theme.bg, fontWeight: "700", fontSize: 16 },
    secondary: { alignItems: "center", padding: 13, borderRadius: 10, backgroundColor: theme.inset, marginTop: 10 },
    secondaryText: { color: theme.ink, fontWeight: "600" },
    rowCard: {
      backgroundColor: theme.card,
      borderColor: theme.border,
      borderWidth: StyleSheet.hairlineWidth,
      borderRadius: 12,
      padding: 10,
      marginBottom: 10,
    },
    rowLine: { flexDirection: "row", gap: 8, marginBottom: 8 },
    cell: {
      backgroundColor: theme.inset,
      borderRadius: 8,
      color: theme.ink,
      fontSize: 15,
      paddingHorizontal: 10,
      paddingVertical: 8,
    },
    cellSymbol: { width: 92, fontWeight: "700" },
    warning: { color: "#e8b33f", fontSize: 12.5 },
  });
}
