// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The one place that talks to the API. Every call goes through here so the
// CSRF token is echoed consistently and the error envelope is unwrapped once.

const API = (function () {
  function csrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)seapath_csrf=([^;]*)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  async function request(method, path, body) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (method !== "GET" && method !== "HEAD") {
      headers["X-CSRF-Token"] = csrfToken();
    }

    const response = await fetch("/api/v1" + path, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });

    return unwrap(response);
  }

  // The body is the file itself, streamed by the browser. A multipart form
  // would mean a copy of a twenty gigabyte VM image for the sake of a name the
  // URL already carries.
  async function upload(path, file) {
    const response = await fetch("/api/v1" + path, {
      method: "PUT",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/octet-stream",
        "X-CSRF-Token": csrfToken(),
      },
      credentials: "same-origin",
      body: file,
    });
    return unwrap(response);
  }

  async function unwrap(response) {
    if (response.status === 204) {
      return null;
    }

    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }

    if (!response.ok) {
      const detail = (payload && payload.error) || {};
      const failure = new Error(detail.message || response.statusText);
      failure.code = detail.code || "error";
      failure.status = response.status;
      // The envelope's detail carries the failing rules, which is the whole
      // value of a refusal: what to fix, rather than that something is wrong.
      failure.detail = detail.detail || {};
      throw failure;
    }
    return payload;
  }

  return {
    get: (path) => request("GET", path),
    post: (path, body) => request("POST", path, body),
    put: (path, body) => request("PUT", path, body),
    del: (path) => request("DELETE", path),
    upload,
    csrfToken,
  };
})();
