export const phoneAlertsSupported = false;
export async function enablePhoneAlerts(): Promise<void> {
  throw new Error("Phone alerts require the installed iOS or Android app.");
}
export async function disablePhoneAlerts(): Promise<void> {}
export function watchNotificationTap(callback: () => void): () => void {
  return () => {};
}
