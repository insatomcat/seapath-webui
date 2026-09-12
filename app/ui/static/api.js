// Copyright (C) 2026, RTE (http://www.rte-france.com)
// SPDX-License-Identifier: Apache-2.0

// The one place that talks to the API. Every call goes through here so the
// CSRF token is echoed consistently and the error envelope is unwrapped once.

// The base is relative on purpose, here and everywhere else the front end
// builds a URL. A reverse proxy can then serve the whole application under a
// prefix without the application being told what the prefix is. It resolves
// correctly because every page of this service sits exactly one segment deep,
// so each of them shares the same base directory. A page nested deeper, or an
// entry point reached without its trailing slash, would break it.

const API = (function () {
  // Which cookie holds this node's token. The name carries a per node suffix,
  // because two ssh tunnels put two nodes on one host name and therefore in
  // one cookie jar, so the page has to say which of the cookies is ours.
  const CSRF_META = document.querySelector('meta[name="csrf-cookie"]');
  const CSRF_COOKIE = CSRF_META ? CSRF_META.content : "seapath_csrf";
  const CSRF_PATTERN = new RegExp("(?:^|;\\s*)" + CSRF_COOKIE + "=([^;]*)");

  function csrfToken() {
    const match = document.cookie.match(CSRF_PATTERN);
    return match ? decodeURIComponent(match[1]) : "";
  }

  // `extra` carries the headers a single call needs and no other does, which
  // today means `If-Match`: a write that names the version it was made against
  // so two browsers cannot silently overwrite each other.
  async function request(method, path, body, extra) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (method !== "GET" && method !== "HEAD") {
      headers["X-CSRF-Token"] = csrfToken();
    }
    Object.assign(headers, extra || {});

    const response = await fetch("api/v1" + path, {
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
  async function upload(path, file, extra) {
    const response = await fetch("api/v1" + path, {
      method: "PUT",
      headers: Object.assign(
        {
          Accept: "application/json",
          "Content-Type": "application/octet-stream",
          "X-CSRF-Token": csrfToken(),
        },
        extra || {}
      ),
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
      const failure = new Error(detail.message || unexplained(response));
      failure.code = detail.code || "error";
      failure.status = response.status;
      // The envelope's detail carries the failing rules, which is the whole
      // value of a refusal: what to fix, rather than that something is wrong.
      failure.detail = detail.detail || {};
      // A session that ended under the page. Every signed in page is served
      // behind the session, so this can only happen after the document
      // arrived: the session expired while the page was open, or somebody
      // signed out in another tab. The way back is the sign in page, and a
      // banner instead leaves an operator reading a refusal they can do
      // nothing about. It used to be the top bar's reading that noticed, which
      // is one request this no longer makes.
      //
      // The sign in page is the one place this must not fire, since a wrong
      // password is a 401 too. Its own bar says which page this is: it has
      // none, and so no way out to send anybody to.
      if (response.status === 401 && document.getElementById("logout")) {
        window.location.assign("login");
      }
      throw failure;
    }
    return payload;
  }

  // A refusal without the envelope was written by something in front of the
  // service, a reverse proxy most of the time, and its status text alone sends
  // the operator looking in the wrong place.
  function unexplained(response) {
    if (response.status === 413) {
      return (
        "The upload was refused before it reached this service (HTTP 413). " +
        "A reverse proxy in front of it limits the size of a request: nginx " +
        "takes one megabyte unless client_max_body_size says otherwise."
      );
    }
    return (
      (response.statusText || "The request failed") +
      " (HTTP " + response.status + ")"
    );
  }

  // A reading an operator asked for says so, and the service scrapes the
  // machines again rather than answering from the few seconds it keeps. The
  // window exists so that drawing one page, and coming back to a page just
  // left, costs the cluster one scrape instead of one per panel; it must never
  // stand between the control on a panel and the machines. Both gestures that
  // mean "now", the control and the timer behind it, come through `reread.js`
  // and carry this. See D45.
  function reading(path, asked) {
    if (!asked) {
      return path;
    }
    return path + (path.includes("?") ? "&" : "?") + "fresh=1";
  }

  return {
    get: (path) => request("GET", path),
    reading,
    post: (path, body) => request("POST", path, body),
    put: (path, body, extra) => request("PUT", path, body, extra),
    del: (path) => request("DELETE", path),
    upload,
    csrfToken,
  };
})();
