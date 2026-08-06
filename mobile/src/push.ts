/**
 * Briefing push notifications.
 *
 * Flow: device asks permission → obtains an Expo push token → registers it
 * with *your* backend (`POST /api/v1/push/register`). When a scheduled
 * briefing finishes, the backend sends one message through Expo's push API.
 * No third party beyond Expo's relay ever sees the token; the backend stores
 * it locally.
 *
 * In Expo Go this works out of the box; standalone store builds additionally
 * need push credentials configured in EAS (see docs/MOBILE-RELEASE.md).
 */

import Constants from "expo-constants";
import * as Device from "expo-device";
import * as Notifications from "expo-notifications";

import { serin } from "./api";

export async function registerForBriefingPush(): Promise<{ ok: boolean; message: string }> {
  if (!Device.isDevice) {
    return { ok: false, message: "Push notifications need a physical device." };
  }
  const existing = await Notifications.getPermissionsAsync();
  let status = existing.status;
  if (status !== "granted") {
    const asked = await Notifications.requestPermissionsAsync();
    status = asked.status;
  }
  if (status !== "granted") {
    return { ok: false, message: "Notification permission was declined." };
  }
  try {
    const projectId: string | undefined =
      Constants.expoConfig?.extra?.eas?.projectId ?? Constants.easConfig?.projectId;
    const token = (await Notifications.getExpoPushTokenAsync(projectId ? { projectId } : undefined)).data;
    await serin.registerPush(token);
    return { ok: true, message: "You'll get a push when a scheduled briefing is ready." };
  } catch (err) {
    return { ok: false, message: err instanceof Error ? err.message : String(err) };
  }
}
