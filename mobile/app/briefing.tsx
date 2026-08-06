/**
 * Briefing reader — latest completed briefing rendered as formatted markdown,
 * plus the daily-schedule controls (same GET/PUT /schedule contract as web).
 */

import { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from "react-native";
import { serin, type Briefing, type Schedule } from "../src/api";
import { Markdown } from "../src/Markdown";
import { useTheme } from "../src/theme";

export default function BriefingReader() {
  const theme = useTheme();
  const styles = makeStyles(theme);
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [timeDraft, setTimeDraft] = useState("");
  const [scheduleNote, setScheduleNote] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const raw = await serin.briefings();
        // The endpoint may return a list or { briefings: [] } shape; tolerate both.
        const list: Briefing[] = Array.isArray(raw) ? raw : (raw as any).briefings || [];
        const latest = list.find(b => b.status === "done");
        if (!latest) {
          setError("No completed briefings yet. Run one from the web app.");
        } else {
          setBriefing(latest);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoading(false);
      }
      try {
        const sched = await serin.schedule();
        setSchedule(sched);
        setTimeDraft(sched.time);
      } catch {
        // Older backends without the /v1/schedule alias — hide the card.
      }
    })();
  }, []);

  const saveSchedule = useCallback(
    async (next: Omit<Schedule, "next_run">) => {
      setScheduleNote("");
      try {
        const saved = await serin.setSchedule(next);
        setSchedule(saved);
        setTimeDraft(saved.time);
        setScheduleNote(
          saved.enabled && saved.next_run
            ? `Next run ${new Date(saved.next_run).toLocaleString()}`
            : saved.enabled
              ? "Scheduled."
              : "Schedule off.",
        );
      } catch (err) {
        setScheduleNote(err instanceof Error ? err.message : String(err));
      }
    },
    [],
  );

  if (loading) {
    return (
      <View style={[styles.container, styles.center]}>
        <ActivityIndicator color={theme.up} />
      </View>
    );
  }

  return (
    <ScrollView style={styles.container} contentContainerStyle={{ padding: 20, paddingBottom: 40 }}>
      {schedule && (
        <View style={styles.scheduleCard}>
          <View style={styles.scheduleRow}>
            <View style={{ flex: 1 }}>
              <Text style={styles.scheduleTitle}>Daily briefing</Text>
              <Text style={styles.scheduleSub}>
                {schedule.enabled ? `Runs every day at ${schedule.time}` : "Off — run manually from the web app"}
              </Text>
            </View>
            <Switch
              value={schedule.enabled}
              trackColor={{ true: theme.up }}
              onValueChange={value => saveSchedule({ ...schedule, enabled: value })}
            />
          </View>
          {schedule.enabled && (
            <View style={styles.scheduleRow}>
              <Text style={styles.scheduleLabel}>Time</Text>
              <TextInput
                style={styles.timeInput}
                value={timeDraft}
                onChangeText={setTimeDraft}
                placeholder="07:30"
                placeholderTextColor={theme.mut}
                autoCapitalize="none"
                keyboardType="numbers-and-punctuation"
              />
              <Pressable
                accessibilityRole="button"
                style={styles.saveButton}
                onPress={() => {
                  if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(timeDraft.trim())) {
                    setScheduleNote("Time must be HH:MM (24h).");
                    return;
                  }
                  saveSchedule({ ...schedule, time: timeDraft.trim() });
                }}
              >
                <Text style={styles.saveText}>Save</Text>
              </Pressable>
            </View>
          )}
          {!!scheduleNote && <Text style={styles.scheduleNote}>{scheduleNote}</Text>}
        </View>
      )}

      {briefing ? (
        <>
          <Text style={styles.meta}>
            {new Date(briefing.created_at).toLocaleString()} · {briefing.model}
          </Text>
          <Markdown text={briefing.output_markdown} theme={theme} />
        </>
      ) : (
        <View style={styles.center}>
          <Text style={styles.body}>{error || "No briefing available."}</Text>
        </View>
      )}
    </ScrollView>
  );
}

function makeStyles(theme: ReturnType<typeof useTheme>) {
  return StyleSheet.create({
    container: { flex: 1, backgroundColor: theme.bg },
    center: { justifyContent: "center", alignItems: "center", padding: 24 },
    meta: {
      color: theme.mut,
      fontSize: 12,
      marginBottom: 16,
      textTransform: "uppercase",
      letterSpacing: 0.6,
    },
    body: { color: theme.sec, fontSize: 15, textAlign: "center" },
    scheduleCard: {
      backgroundColor: theme.card,
      borderColor: theme.border,
      borderWidth: StyleSheet.hairlineWidth,
      borderRadius: 14,
      padding: 16,
      marginBottom: 20,
      gap: 12,
    },
    scheduleRow: { flexDirection: "row", alignItems: "center", gap: 12 },
    scheduleTitle: { color: theme.ink, fontSize: 15, fontWeight: "700" },
    scheduleSub: { color: theme.mut, fontSize: 13, marginTop: 2 },
    scheduleLabel: { color: theme.sec, fontSize: 14, fontWeight: "600" },
    timeInput: {
      flex: 1,
      backgroundColor: theme.inset,
      borderColor: theme.border,
      borderWidth: StyleSheet.hairlineWidth,
      borderRadius: 10,
      paddingHorizontal: 12,
      paddingVertical: 8,
      color: theme.ink,
      fontSize: 15,
    },
    saveButton: {
      backgroundColor: theme.up,
      borderRadius: 10,
      paddingHorizontal: 16,
      paddingVertical: 9,
    },
    saveText: { color: theme.bg, fontWeight: "700", fontSize: 14 },
    scheduleNote: { color: theme.mut, fontSize: 12.5 },
  });
}
