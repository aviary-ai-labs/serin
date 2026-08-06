/**
 * Settings — pairing + security for a *client* app.
 *
 * Three ways to connect: scan the pairing QR from the web app (Connectors →
 * Data → Pair mobile), type the URL, or paste URL + token by hand. Also
 * hosts the biometric app-lock toggle and briefing push notifications.
 */

import { useEffect, useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, Switch, Text, TextInput, View } from "react-native";
import { useRouter } from "expo-router";
import { CameraView, useCameraPermissions } from "expo-camera";
import * as LocalAuthentication from "expo-local-authentication";

import {
  serin,
  getBackendUrl,
  getBiometricLock,
  getToken,
  setBackendUrl,
  setBiometricLock,
  setToken,
} from "../src/api";
import { registerForBriefingPush } from "../src/push";
import { useTheme } from "../src/theme";

export default function Settings() {
  const router = useRouter();
  const theme = useTheme();
  const [url, setUrl] = useState("http://192.168.1.10:8890");
  const [token, setLocalToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [biometric, setBiometric] = useState(false);
  const [biometricAvailable, setBiometricAvailable] = useState(false);
  const [pushBusy, setPushBusy] = useState(false);
  const [permission, requestPermission] = useCameraPermissions();

  useEffect(() => {
    (async () => {
      const stored = await getBackendUrl();
      if (stored) setUrl(stored);
      const t = await getToken();
      if (t) setLocalToken(t);
      setBiometric(await getBiometricLock());
      const hardware = await LocalAuthentication.hasHardwareAsync();
      const enrolled = await LocalAuthentication.isEnrolledAsync();
      setBiometricAvailable(hardware && enrolled);
    })();
  }, []);

  async function save() {
    setBusy(true);
    try {
      await setBackendUrl(url);
      await setToken(token.trim());
      const probe = await serin.version();
      Alert.alert("Connected", `Serin ${probe.build} (API v${probe.api_version})`, [
        { text: "OK", onPress: () => router.back() },
      ]);
    } catch (err) {
      Alert.alert("Couldn't reach Serin", err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function startScan() {
    if (!permission?.granted) {
      const asked = await requestPermission();
      if (!asked.granted) {
        Alert.alert("Camera needed", "Allow camera access to scan the pairing QR.");
        return;
      }
    }
    setScanning(true);
  }

  function onScanned(data: string) {
    setScanning(false);
    try {
      const parsed = JSON.parse(data) as { serin?: number; url?: string; token?: string };
      if (!parsed.serin || !parsed.url) throw new Error("not a Serin pairing code");
      setUrl(parsed.url);
      setLocalToken(parsed.token || "");
      Alert.alert("Pairing code scanned", "Tap “Save & test” to connect.");
    } catch {
      Alert.alert("Unrecognized QR", "Scan the pairing code from Serin web → Connectors → Data → Pair mobile.");
    }
  }

  async function toggleBiometric(next: boolean) {
    if (next) {
      const result = await LocalAuthentication.authenticateAsync({ promptMessage: "Confirm to enable app lock" });
      if (!result.success) return;
    }
    setBiometric(next);
    await setBiometricLock(next);
  }

  async function enablePush() {
    setPushBusy(true);
    try {
      const outcome = await registerForBriefingPush();
      Alert.alert(
        outcome.ok ? "Notifications on" : "Not available",
        outcome.message,
      );
    } finally {
      setPushBusy(false);
    }
  }

  const styles = makeStyles(theme);

  if (scanning) {
    return (
      <View style={{ flex: 1, backgroundColor: "#000" }}>
        <CameraView
          style={{ flex: 1 }}
          barcodeScannerSettings={{ barcodeTypes: ["qr"] }}
          onBarcodeScanned={(event) => onScanned(event.data)}
        />
        <Pressable accessibilityRole="button" style={styles.scanCancel} onPress={() => setScanning(false)}>
          <Text style={styles.primaryText}>Cancel</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <ScrollView style={styles.container} contentContainerStyle={{ padding: 20, paddingBottom: 48 }}>
      <Pressable accessibilityRole="button" style={styles.qrButton} onPress={startScan}>
        <Text style={styles.qrButtonText}>▣ Scan pairing QR</Text>
      </Pressable>
      <Text style={styles.help}>Serin web → Connectors → Data → “Pair mobile app”.</Text>

      <Text style={[styles.label, { marginTop: 26 }]}>Backend URL</Text>
      <TextInput
        value={url}
        onChangeText={setUrl}
        autoCapitalize="none"
        autoCorrect={false}
        keyboardType="url"
        placeholder="http://your-serin:8890"
        placeholderTextColor={theme.mut}
        style={styles.input}
        accessibilityLabel="Backend URL"
      />
      <Text style={styles.help}>
        Self-hosted? Use your LAN IP or Tailscale hostname (port 8890). Remote hosts should sit
        behind HTTPS.
      </Text>

      <Text style={[styles.label, { marginTop: 26 }]}>Bearer token</Text>
      <TextInput
        value={token}
        onChangeText={setLocalToken}
        autoCapitalize="none"
        autoCorrect={false}
        secureTextEntry
        placeholder="required when the app lock is on"
        placeholderTextColor={theme.mut}
        style={styles.input}
        accessibilityLabel="Bearer token"
      />
      <Text style={styles.help}>
        Set SERIN_AUTH_PASSWORD on the backend, sign in on the web, and the pairing QR carries this
        token for you.
      </Text>

      <Pressable accessibilityRole="button" style={[styles.primary, busy && { opacity: 0.5 }]} disabled={busy} onPress={save}>
        <Text style={styles.primaryText}>{busy ? "Connecting…" : "Save & test"}</Text>
      </Pressable>

      <View style={styles.divider} />

      <View style={styles.switchRow}>
        <View style={{ flex: 1 }}>
          <Text style={styles.switchTitle}>Require Face ID / biometrics</Text>
          <Text style={styles.help}>
            {biometricAvailable
              ? "Lock the app behind the device's biometric check."
              : "No biometric hardware / enrollment found on this device."}
          </Text>
        </View>
        <Switch
          value={biometric}
          disabled={!biometricAvailable}
          onValueChange={toggleBiometric}
          accessibilityLabel="Require biometrics"
        />
      </View>

      <View style={styles.divider} />

      <Text style={styles.switchTitle}>Briefing notifications</Text>
      <Text style={styles.help}>
        Get a push when your morning briefing is ready. Uses Expo's push service; the token is
        stored on your own backend only.
      </Text>
      <Pressable accessibilityRole="button" style={[styles.secondary, pushBusy && { opacity: 0.5 }]} disabled={pushBusy} onPress={enablePush}>
        <Text style={styles.secondaryText}>{pushBusy ? "Registering…" : "Enable notifications"}</Text>
      </Pressable>
    </ScrollView>
  );
}

function makeStyles(theme: ReturnType<typeof useTheme>) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.bg },
    label: { color: theme.mut, fontSize: 12, textTransform: "uppercase", letterSpacing: 0.8, marginBottom: 8 },
    input: {
      backgroundColor: theme.inset,
      color: theme.ink,
      padding: 12,
      borderRadius: 10,
      fontSize: 16,
      borderColor: theme.border,
      borderWidth: StyleSheet.hairlineWidth,
    },
    help: { color: theme.mut, fontSize: 12, marginTop: 8, lineHeight: 18 },
    primary: { backgroundColor: theme.up, paddingVertical: 14, borderRadius: 10, marginTop: 24, alignItems: "center" },
    primaryText: { color: theme.bg, fontWeight: "700", fontSize: 16 },
    secondary: { alignItems: "center", padding: 13, borderRadius: 10, backgroundColor: theme.inset, marginTop: 12 },
    secondaryText: { color: theme.ink, fontWeight: "600" },
    qrButton: {
      borderColor: theme.up,
      borderWidth: 1.5,
      borderStyle: "dashed",
      borderRadius: 12,
      alignItems: "center",
      paddingVertical: 18,
    },
    qrButtonText: { color: theme.up, fontSize: 16, fontWeight: "700" },
    divider: { height: StyleSheet.hairlineWidth, backgroundColor: theme.border, marginVertical: 24 },
    switchRow: { flexDirection: "row", alignItems: "center", gap: 14 },
    switchTitle: { color: theme.ink, fontSize: 15, fontWeight: "600" },
    scanCancel: {
      position: "absolute",
      bottom: 48,
      alignSelf: "center",
      backgroundColor: "rgba(0,0,0,0.6)",
      paddingHorizontal: 26,
      paddingVertical: 12,
      borderRadius: 999,
    },
  });
}
