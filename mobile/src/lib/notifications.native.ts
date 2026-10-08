import { Platform } from "react-native";
import * as Notifications from "expo-notifications";
import Constants from "expo-constants";
import * as SecureStore from "expo-secure-store";
import { api } from "./api";
export const phoneAlertsSupported = true;
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldPlaySound: true,
    shouldSetBadge: false,
    shouldShowBanner: true,
    shouldShowList: true,
  }),
});
export async function enablePhoneAlerts() {
  const projectId =
    Constants.expoConfig?.extra?.eas?.projectId ??
    Constants.easConfig?.projectId;
  if (!projectId)
    throw new Error(
      "Phone notifications need the signed Orion build with its EAS project configured.",
    );
  if (Platform.OS === "android")
    await Notifications.setNotificationChannelAsync("orion-attention", {
      name: "Connection alerts",
      importance: Notifications.AndroidImportance.HIGH,
    });
  let permissions = await Notifications.getPermissionsAsync();
  if (!permissions.granted)
    permissions = await Notifications.requestPermissionsAsync();
  if (!permissions.granted)
    throw new Error(
      "Notifications are disabled. Allow Orion notifications in phone settings.",
    );
  const token = (await Notifications.getExpoPushTokenAsync({ projectId })).data;
  await api("/push-device", { token }, "PUT");
  await SecureStore.setItemAsync("orion-push-token", token);
}
export async function disablePhoneAlerts() {
  const token = await SecureStore.getItemAsync("orion-push-token");
  if (token) await api("/push-device", { token }, "DELETE");
  await SecureStore.deleteItemAsync("orion-push-token");
}
export function watchNotificationTap(callback: () => void) {
  const last = Notifications.getLastNotificationResponse();
  if (last?.notification.request.content.data?.screen === "attention") {
    callback();
    Notifications.clearLastNotificationResponseAsync();
  }
  const sub = Notifications.addNotificationResponseReceivedListener((r) => {
    if (r.notification.request.content.data?.screen === "attention") {
      callback();
      Notifications.clearLastNotificationResponseAsync();
    }
  });
  return () => sub.remove();
}
