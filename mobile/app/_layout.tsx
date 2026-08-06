/**
 * Root layout — expo-router stack + the biometric app-lock gate.
 *
 * When the user enabled "Require Face ID" in Settings, the whole navigator
 * hides behind a lock view until the device auth succeeds (re-armed every
 * cold start).
 */

import { useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, View, useColorScheme } from "react-native";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { SafeAreaProvider } from "react-native-safe-area-context";
import * as LocalAuthentication from "expo-local-authentication";

import { getBiometricLock } from "../src/api";
import { DARK, LIGHT } from "../src/theme";

export default function RootLayout() {
  const scheme = useColorScheme();
  const theme = scheme === "light" ? LIGHT : DARK;
  const [gate, setGate] = useState<"checking" | "locked" | "open">("checking");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const required = await getBiometricLock();
      if (cancelled) return;
      if (!required) {
        setGate("open");
        return;
      }
      setGate("locked");
      const result = await LocalAuthentication.authenticateAsync({ promptMessage: "Unlock Serin" });
      if (!cancelled) setGate(result.success ? "open" : "locked");
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function unlock() {
    const result = await LocalAuthentication.authenticateAsync({ promptMessage: "Unlock Serin" });
    setGate(result.success ? "open" : "locked");
  }

  if (gate !== "open") {
    return (
      <SafeAreaProvider>
        <StatusBar style={scheme === "light" ? "dark" : "light"} />
        <View style={[styles.lock, { backgroundColor: theme.bg }]}>
          <Text style={[styles.lockTitle, { color: theme.ink }]}>Serin</Text>
          {gate === "locked" && (
            <Pressable
              accessibilityRole="button"
              style={[styles.lockButton, { backgroundColor: theme.up }]}
              onPress={unlock}
            >
              <Text style={[styles.lockButtonText, { color: theme.bg }]}>Unlock</Text>
            </Pressable>
          )}
        </View>
      </SafeAreaProvider>
    );
  }

  return (
    <SafeAreaProvider>
      <StatusBar style={scheme === "light" ? "dark" : "light"} />
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: theme.card },
          headerTintColor: theme.ink,
          headerTitleStyle: { fontWeight: "600" },
          contentStyle: { backgroundColor: theme.bg },
        }}
      >
        <Stack.Screen name="index" options={{ title: "Serin" }} />
        <Stack.Screen name="briefing" options={{ title: "Briefing" }} />
        <Stack.Screen name="news" options={{ title: "News" }} />
        <Stack.Screen name="xray" options={{ title: "Portfolio X-ray" }} />
        <Stack.Screen name="connectors" options={{ title: "Connectors" }} />
        <Stack.Screen name="position/[id]" options={{ title: "Position" }} />
        <Stack.Screen name="add-position" options={{ title: "Add position", presentation: "modal" }} />
        <Stack.Screen name="smart-import" options={{ title: "Smart Import", presentation: "modal" }} />
        <Stack.Screen name="settings" options={{ title: "Settings", presentation: "modal" }} />
      </Stack>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  lock: { flex: 1, alignItems: "center", justifyContent: "center", gap: 18 },
  lockTitle: { fontSize: 28, fontWeight: "800", letterSpacing: -0.5 },
  lockButton: { paddingHorizontal: 28, paddingVertical: 12, borderRadius: 10 },
  lockButtonText: { fontWeight: "700", fontSize: 16 },
});
