import { Platform } from "react-native";
import * as SecureStore from "expo-secure-store";
const native = Platform.OS !== "web";
const base = native
  ? (process.env.EXPO_PUBLIC_API_URL || "").replace(/\/$/, "")
  : "";
let token = "",
  csrf = "";
export async function restore() {
  if (native) token = (await SecureStore.getItemAsync("orion-session")) || "";
}
export async function api(
  path: string,
  body?: unknown,
  method = "POST",
): Promise<any> {
  if (native && !base.startsWith("https://"))
    throw new Error(
      "Configure the secure Orion API address before building this app.",
    );
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 55000);
  try {
    const r = await fetch(base + "/api" + path, {
      method: body === undefined ? "GET" : method,
      credentials: native ? "omit" : "same-origin",
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(native && token ? { Authorization: "Bearer " + token } : {}),
        ...(!native ? { "X-CSRF-Token": csrf } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Request failed");
    if (data.csrf) csrf = data.csrf;
    return data;
  } finally {
    clearTimeout(timer);
  }
}
export async function login(username: string, password: string) {
  const d = await api(native ? "/mobile/login" : "/login", {
    username,
    password,
  });
  if (native) {
    token = d.token;
    await SecureStore.setItemAsync("orion-session", token);
  }
}
export const register = (username: string, password: string) =>
  api(native ? "/mobile/register" : "/register", { username, password });
export async function logout() {
  try {
    await api("/logout", {});
  } finally {
    token = "";
    csrf = "";
    if (native) await SecureStore.deleteItemAsync("orion-session");
  }
}
