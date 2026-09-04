(() => {
  const params = new URLSearchParams(window.location.search);
  const jobId = params.get("job") || params.get("gid") || params.get("id");
  const prompt = params.get("prompt") || "";
  let summary = params.get("summary") || "";
  const iframeContainer = document.getElementById("iframe-container");
  const chats = document.getElementById("chats");
  const chatForm = document.getElementById("chatForm");
  const chatInput = document.getElementById("chatInput");
  const sendChat = document.getElementById("sendChat");
  let history = [];
  let isGeneratingReply = false;
  let isLeavingPage = false;
  let hasVisualization = true;

  const userId = () => {
    let id = localStorage.getItem("quantacanvas_user_id");
    if (!id && window.crypto?.randomUUID) {
      id = window.crypto.randomUUID();
      localStorage.setItem("quantacanvas_user_id", id);
    }
    return id || "";
  };

  window.addEventListener("beforeunload", (event) => {
    if (isLeavingPage || !hasVisualization) return;

    event.preventDefault();
    event.returnValue = "Your visualization will remain available if you reload, but leaving may end this session.";
  });

  const leavePage = async (destination = "../frontend/tool.html") => {
    if (isLeavingPage) return;
    isLeavingPage = true;
    window.location.assign(destination);
  };

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || link.target === "_blank" || event.defaultPrevented) return;
    const destination = new URL(link.href, window.location.href);
    if (destination.href === window.location.href || destination.hash) return;
    event.preventDefault();
    if (hasVisualization) {
      const confirmed = window.confirm(
        "Leaving this page will end your session with this visualization. Continue?"
      );
      if (!confirmed) return;
    }
    leavePage(destination.href);
  });

  const updateSendState = () => {
    sendChat.disabled = isGeneratingReply || !chatInput.value.trim();
    chatInput.disabled = isGeneratingReply;
  };

  chatInput.addEventListener("input", updateSendState);
  chatInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!sendChat.disabled) chatForm.requestSubmit();
    }
  });
  updateSendState();

  if (!jobId) {
    window.location.replace("/tool.html");
    return;
  }

  const gid = params.get("gid") || jobId;

  const confirmGeneration = () => {
    fetch("/api/generation/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ generation_id: gid, user_id: userId(), prompt }),
    })
      .then((r) => r.json())
      .then(() => {
        const counter = document.getElementById("genCounter");
        if (!counter) return;
        const uid = localStorage.getItem("quantacanvas_user_id") || "anonymous";
        fetch(`/api/generations/remaining?user_id=${encodeURIComponent(uid)}`)
          .then((r) => r.json())
          .then((data) => {
            if (typeof data.used !== "number") return;
            const starSvg = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2L9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61z"/></svg>`;
            counter.innerHTML = `${starSvg}<span>${data.used}/${data.limit}</span>`;
            counter.title = `${data.used}/${data.limit} generations today (resets at midnight)`;
          })
          .catch(() => {});
      })
      .catch(() => {});
  };

  // ---- waiting screen -------------------------------------------------------
  // The document is produced by the GitHub Actions worker, so we poll for it.
  // This link is durable: reloading, closing the tab, or opening it later works.
  const POLL_MS = 3000;
  const GIVE_UP_MS = 30 * 60 * 1000;
  const startedAt = Date.now();
  const waitingMessages = [
    "Reading your question…",
    "Sketching the animation…",
    "Working out the details…",
    "Labelling the diagram…",
    "Polishing the final frame…",
  ];

  const status = document.createElement("div");
  status.className = "qc-generation-status";
  status.innerHTML = `
    <div class="qc-spinner" aria-hidden="true"></div>
    <p class="qc-status-title">Building your visualization…</p>
    <p class="qc-status-note">This usually takes a couple of minutes. You can leave this page open, reload it, or come back to this link later — the result is saved.</p>
  `;
  iframeContainer.replaceChildren(status);
  const statusTitle = status.querySelector(".qc-status-title");
  let messageIndex = 0;
  let messageTimer = window.setInterval(() => {
    messageIndex = (messageIndex + 1) % waitingMessages.length;
    statusTitle.textContent = waitingMessages[messageIndex];
  }, 6000);

  const stopWaitingAnimation = () => {
    if (messageTimer) window.clearInterval(messageTimer);
    messageTimer = null;
  };

  const showError = (message) => {
    stopWaitingAnimation();
    hasVisualization = false;
    status.innerHTML = `
      <p class="qc-status-title">That didn't work</p>
      <p class="qc-status-note"></p>
      <a class="btn" href="/tool.html">Try another prompt</a>
    `;
    status.querySelector(".qc-status-note").textContent = message;
  };

  const renderVisualization = (job) => {
    stopWaitingAnimation();
    summary = job.summary || summary;
    const iframe = document.createElement("iframe");
    iframe.srcdoc = job.html;
    iframe.title = "Generated QuantaCanvas visualization";
    iframe.setAttribute("sandbox", "allow-scripts allow-same-origin");
    iframe.addEventListener("load", confirmGeneration, { once: true });
    iframeContainer.replaceChildren(iframe);
  };

  const poll = async () => {
    try {
      const response = await fetch(`/api/generation/${encodeURIComponent(jobId)}`, {
        cache: "no-store",
      });
      const job = await response.json();

      if (!response.ok) {
        showError(job.error || "We couldn't find that visualization.");
        return;
      }
      if (job.status === "completed") {
        renderVisualization(job);
        return;
      }
      if (job.status === "failed" || job.status === "expired") {
        showError(job.error || "The visualization could not be generated.");
        return;
      }
      if (Date.now() - startedAt > GIVE_UP_MS) {
        showError("This is taking longer than expected. Please try generating it again.");
        return;
      }
      window.setTimeout(poll, POLL_MS);
    } catch (error) {
      // Network blip: keep waiting rather than losing the job.
      window.setTimeout(poll, POLL_MS * 2);
    }
  };

  poll();

  const addMessage = (role, text) => {
    const bubble = document.createElement("div");
    bubble.className = role === "user" ? "chats-me" : "chats-bot";
    text.split(/\n\s*\n/).forEach((paragraph) => {
      const element = document.createElement("p");
      element.textContent = paragraph;
      bubble.appendChild(element);
    });
    chats.appendChild(bubble);
    chats.scrollTop = chats.scrollHeight;

    renderMathWhenReady(bubble);
    return bubble;
  };

  const renderMathWhenReady = (element) => {
    if (window.renderMathInElement) {
      window.renderMathInElement(element, {
        delimiters: [
          { left: "\\[", right: "\\]", display: true },
          { left: "\\(", right: "\\)", display: false },
          { left: "$$", right: "$$", display: true },
          { left: "$", right: "$", display: false },
        ],
        throwOnError: false,
        strict: "ignore",
        trust: true,
      });
    } else {
      window.setTimeout(() => renderMathWhenReady(element), 50);
    }
  };

  const setReplyContent = (bubble, text) => {
    bubble.replaceChildren();
    const paragraphs = text.split(/\n\s*\n/).filter((paragraph) => paragraph.trim());
    for (const paragraph of paragraphs) {
      const element = document.createElement("p");
      element.textContent = paragraph;
      bubble.appendChild(element);
    }
  };

  addMessage("bot", "Your visualization is ready. Ask me anything about it.");

  chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = chatInput.value.trim();
    if (!question || isGeneratingReply) return;
    isGeneratingReply = true;
    updateSendState();
    chatInput.value = "";
    addMessage("user", question);
    history.push({ role: "user", text: question });
    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          user_id: userId(),
          summary: summary || `The lesson was generated for this request: ${prompt}`,
          history,
        }),
      });
      if (!response.ok) {
        const result = await response.json();
        throw new Error(result.error || "Follow-up failed.");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let reply = "";
      const bubble = addMessage("bot", "");

      const processEvent = (eventText) => {
        const line = eventText.split("\n").find((entry) => entry.startsWith("data: "));
        if (!line) return false;
        const payload = line.slice(6);
        if (payload === "[DONE]") return true;
        const event = JSON.parse(payload);
        if (event.error) throw new Error(event.error);
        reply += event.text || "";
        bubble.textContent = reply;
        chats.scrollTop = chats.scrollHeight;
        return false;
      };

      let finished = false;
      while (!finished) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const events = buffer.split("\n\n");
        buffer = events.pop() || "";
        for (const eventText of events) finished = processEvent(eventText) || finished;
        if (done) break;
      }
      if (buffer.trim()) processEvent(buffer);
      setReplyContent(bubble, reply);
      renderMathWhenReady(bubble);
      history.push({ role: "bot", text: reply });
    } catch (error) {
      if (!navigator.onLine || error instanceof TypeError) {
        addMessage("bot", "Connection lost.Check you internet and try again.")
      } else {
        addMessage("bot", error.message);
      }
    } finally {
      isGeneratingReply = false;
      updateSendState();
      chatInput.focus();
    }
  });
})();
