(() => {
  const params = new URLSearchParams(window.location.search);
  const id = params.get("id");
  const html = id ? sessionStorage.getItem(`qc-viz-${id}`) : null;
  const prompt = params.get("prompt") || "";
  const summary = params.get("summary") || "";
  const iframeContainer = document.getElementById("iframe-container");
  const chats = document.getElementById("chats");
  const chatForm = document.getElementById("chatForm");
  const chatInput = document.getElementById("chatInput");
  const sendChat = document.getElementById("sendChat");
  const newTopic = document.getElementById("newTopic");
  let history = [];
  let isGeneratingReply = false;
  let isLeavingPage = false;

  window.addEventListener("beforeunload", (event) => {
    if (isLeavingPage) return;
    event.preventDefault();
    event.returnValue = "Your visualization will remain available if you reload, but leaving may end this session.";
  });

  const leavePage = async (destination = "/tool.html") => {
    if (isLeavingPage) return;
    isLeavingPage = true;
    if (id) sessionStorage.removeItem(`qc-viz-${id}`);
    window.location.assign(destination);
  };

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]");
    if (!link || link.target === "_blank" || event.defaultPrevented) return;
    const destination = new URL(link.href, window.location.href);
    if (destination.href === window.location.href || destination.hash) return;
    event.preventDefault();
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

  if (!html) {
    window.location.replace("/tool.html");
    return;
  }

  const iframe = document.createElement("iframe");
  iframe.srcdoc = html;
  iframe.title = "Generated QuantaCanvas visualization";
  iframeContainer.appendChild(iframe);

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
      addMessage("bot", error.message);
    } finally {
      isGeneratingReply = false;
      updateSendState();
      chatInput.focus();
    }
  });

  newTopic.addEventListener("click", leavePage);
})();
