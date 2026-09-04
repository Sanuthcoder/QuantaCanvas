const showToast = (message, type = "info") => {
		let stack = document.querySelector(".qc-toast-stack");
		if (!stack) {
			stack = document.createElement("div");
			stack.className = "qc-toast-stack";
			document.body.appendChild(stack);
		}
		const toast = document.createElement("div");
		toast.className = `qc-toast qc-toast-${type}`;
		toast.textContent = message;
		stack.appendChild(toast);
		setTimeout(() => toast.remove(), 4500);
		setTimeout(() => {
			if (!stack.children.length) stack.remove();
		}, 4600);
	};

window.addEventListener("offline", () => {
	showToast("Connection lost. Check your internet connection.", "negative");
});

window.addEventListener("online", () => {
	showToast("Connection restored.");
});

(() => {
	const CONSENT_KEY = "qc-consent-analytics";
	const stored = localStorage.getItem(CONSENT_KEY);
 
	const applyConsent = (granted) => {
		if (typeof window.gtag === "function") {
			window.gtag("consent", "update", {
				analytics_storage: granted ? "granted" : "denied",
			});
		}
	};
 
	if (stored === "granted" || stored === "denied") {
		applyConsent(stored === "granted");
		return;
	}
 
	const banner = document.createElement("div");
	banner.className = "qc-consent-banner";
	banner.innerHTML = `
		<p>We use cookies for analytics to understand how QuantaCanvas is used. You can accept or decline — see our <a href="privacy.html">privacy notice</a>.</p>
		<div class="qc-consent-actions">
			<button type="button" class="btn-ghost" data-consent="decline">Decline</button>
			<button type="button" class="btn" data-consent="accept">Accept</button>
		</div>
	`;
	document.body.appendChild(banner);
 
	banner.addEventListener("click", (event) => {
		const choice = event.target.dataset.consent;
		if (!choice) return;
		const granted = choice === "accept";
		localStorage.setItem(CONSENT_KEY, granted ? "granted" : "denied");
		applyConsent(granted);
		banner.remove();
	});
})();

const parseJsonResponse = async (response) => {
	const raw = await response.text();
	try {
		return JSON.parse(raw);
	} catch {
		console.error("Non-JSON response received:", raw.slice(0, 300));
		return {
			error: response.status === 504 || !response.ok
				? "The server took too long or hit an error. Please try again — if it keeps happening, try a simpler prompt."
				: "Something went wrong. Please try again.",
		};
	}
};

(() => {
	const promptForm = document.getElementById("promptForm");
	if (!promptForm) return;

	const homeView = document.getElementById("toolHomeView");
	const promptInput = document.getElementById("promptInput");
	const overlay = document.getElementById("toolOverlay");
	const loadingMessage = document.getElementById("loadingMessage");
	const abortButton = document.getElementById("abortGeneration");
	 const sendPrompt = document.getElementById("sendPrompt");
	let requestController = null;
	let loadingMessageTimer = null;
	let lessonContext = "";
	let history = [];

	 const typewriterPhrases = [
		 "Explain how volume of revolution works...",
		 "Visualize the Pythagorean theorem...",
		 "Show me how a Fourier transform works...",
		 "Explain integration geometrically...",
		 "Visualize how a neural network learns...",
	 ];
	 let phraseIndex = 0;
	 let typewriterTimer;
	 let typewriterRunning = true;
	 let typewriterRunId = 0;

	 const typePlaceholder = async (runId) => {
		 while (typewriterRunning && runId === typewriterRunId) {
			 const phrase = typewriterPhrases[phraseIndex];
			 for (let index = 0; index <= phrase.length && typewriterRunning && runId === typewriterRunId; index += 1) {
				 if (promptInput.value) break;
				 promptInput.placeholder = phrase.slice(0, index);
				 await new Promise((resolve) => { typewriterTimer = setTimeout(resolve, 60); });
			 }
			 await new Promise((resolve) => { typewriterTimer = setTimeout(resolve, 1500); });
			 for (let index = phrase.length; index >= 0 && typewriterRunning && runId === typewriterRunId; index -= 1) {
				 if (promptInput.value) break;
				 promptInput.placeholder = phrase.slice(0, index);
				 await new Promise((resolve) => { typewriterTimer = setTimeout(resolve, 30); });
			 }
			 await new Promise((resolve) => { typewriterTimer = setTimeout(resolve, 300); });
			 phraseIndex = (phraseIndex + 1) % typewriterPhrases.length;
		 }
	 };

	 const startTypewriter = () => {
		 if (typewriterRunning) return;
		 typewriterRunning = true;
		 typewriterRunId += 1;
		 typePlaceholder(typewriterRunId);
	 };

	 

	 const stopTypewriter = () => {
		 typewriterRunning = false;
		 typewriterRunId += 1;
		 clearTimeout(typewriterTimer);
	 };

	 const updateSendState = () => {
		 sendPrompt.disabled = !promptInput.value.trim();
	 };

	 const urlPrompt = new URLSearchParams(window.location.search).get("prompt");
	 if (urlPrompt) {
	   promptInput.value = urlPrompt;
	   updateSendState();
	   stopTypewriter();
	   promptInput.placeholder = "";
	 }

	 promptInput.addEventListener("input", () => {
		 updateSendState();
		 if (promptInput.value.trim()) {
			 stopTypewriter();
		 } else {
			 startTypewriter();
		 }
	 });
	 typePlaceholder(typewriterRunId);

	 

	const userId = () => {
		let id = localStorage.getItem("quantacanvas_user_id");
		if (!id) {
			if (window.crypto?.randomUUID) {
				id = window.crypto.randomUUID();
			} else if (window.crypto?.getRandomValues) {
				const bytes = new Uint8Array(16);
				window.crypto.getRandomValues(bytes);
				bytes[6] = (bytes[6] & 0x0f) | 0x40;
				bytes[8] = (bytes[8] & 0x3f) | 0x80;
				id = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"))
					.join("")
					.replace(/(.{8})(.{4})(.{4})(.{4})(.{12})/, "$1-$2-$3-$4-$5");
			} else {
				id = `user-${Date.now()}-${Math.random().toString(36).slice(2)}`;
			}
			localStorage.setItem("quantacanvas_user_id", id);
		}
		return id;
	};

	const warnBeforeUnload = (event) => {
		event.preventDefault();
		event.returnValue = "";
		return "";
	};

	const setLoading = (loading) => {
		overlay.hidden = !loading;
		promptInput.disabled = loading;
		 sendPrompt.disabled = loading || !promptInput.value.trim();
		 if (loading) {
			 let dots = 0;
			 loadingMessage.textContent = "Generating visualization";
			 loadingMessageTimer = setInterval(() => {
				dots = (dots + 1) % 4;
				loadingMessage.textContent = `Generating visualization${".".repeat(dots)}`;
			 }, 500);
		 } else {
			 window.removeEventListener("beforeunload", warnBeforeUnload);
			 if (loadingMessageTimer) {
				 clearInterval(loadingMessageTimer);
				 loadingMessageTimer = null;
			 }
		 }
	};

	

	// Queued flow: /api/generate returns a job id immediately and the responses
	// page polls until the GitHub Actions worker has written the document.
	promptForm.addEventListener("submit", async (event) => {
		event.preventDefault();
		const prompt = promptInput.value.trim();
		if (!prompt) return;
		requestController = new AbortController();
		setLoading(true);
		try {
			const response = await fetch("/api/generate", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ prompt, user_id: userId() }),
				signal: requestController.signal,
			});
			const result = await parseJsonResponse(response);
			if (!response.ok) throw new Error(result.error || "Generation failed.");
			history = [];
			const params = new URLSearchParams({
				job: result.job_id,
				gid: result.generation_id || result.job_id,
				prompt,
			});
			// Durable link: reload it, close the tab, open it on another device later.
			window.location.assign(`/responses?${params.toString()}`);
		} catch (error) {
			if (error.name !== "AbortError") {
				showToast(error.message, "negative");
			} else {
				showToast("Generation cancelled.");
			}
			setLoading(false);
		} finally {
			requestController = null;
		}
	});

	abortButton.addEventListener("click", () => requestController?.abort());
})();

document.querySelectorAll("[data-chip]").forEach((chip) => {
  const go = () => {
    const text = chip.textContent.trim();
    window.location.href = `/tool.html?prompt=${encodeURIComponent(text)}`;
  };
  chip.addEventListener("click", go);
  chip.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") go(); });
});

(() => {
  const navMenu = document.getElementById("navMenu");
  const navLinks = document.getElementById("navLinks");
  const moreBtn = document.getElementById("moreBtn");
  const moreMenu = document.getElementById("moreMenu");
  if (!navMenu || !navLinks || !moreBtn || !moreMenu) return;

  const MOBILE_QUERY = window.matchMedia("(max-width: 760px)");

  const cloneIntoMoreMenu = (li) => {
    const a = li.querySelector("a");
    if (!a) return;
    const clone = document.createElement("li");
    const link = document.createElement("a");
    link.href = a.href;
    link.textContent = a.textContent;
    clone.appendChild(link);
    moreMenu.appendChild(clone);
  };

  const syncOverflow = () => {
    const items = [...navLinks.querySelectorAll("li")];

    moreMenu.innerHTML = "";
    moreBtn.style.display = "none";
    items.forEach((li) => {
      li.style.display = "";
    });

    if (MOBILE_QUERY.matches) {
      items.forEach((li) => {
        cloneIntoMoreMenu(li);
      });
      moreBtn.style.display = "inline-flex";
      return;
    }

    
    const navRight = navMenu.getBoundingClientRect().right;

    
    let overflowing = items.filter(
      (li) => li.getBoundingClientRect().right > navRight
    );

    if (!overflowing.length) return;

    moreBtn.style.display = "inline-flex";
    const moreLeft = moreBtn.getBoundingClientRect().left;

    overflowing = items.filter(
      (li) => li.getBoundingClientRect().right > moreLeft
    );

    overflowing.forEach((li) => {
      li.style.display = "none";
      cloneIntoMoreMenu(li);
    });

    if (!moreMenu.children.length) {
      moreBtn.style.display = "none";
    }
  };

  moreBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = moreMenu.classList.toggle("open");
    moreBtn.setAttribute("aria-expanded", String(open));
  });

  document.addEventListener("click", () => {
    moreMenu.classList.remove("open");
    moreBtn.setAttribute("aria-expanded", "false");
  });

  moreMenu.addEventListener("click", (e) => e.stopPropagation());

  const handleResize = () => {
    requestAnimationFrame(syncOverflow);
  };

  const ro = new ResizeObserver(handleResize);
  ro.observe(navMenu);

  window.addEventListener("resize", handleResize);
  MOBILE_QUERY.addEventListener("change", handleResize);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", handleResize);
  } else {
    handleResize();
  }

  if (document.fonts) {
    document.fonts.ready.then(handleResize);
  }
})();

(() => {
  const lightbox = document.getElementById("lightbox");
  const openBtn = document.getElementById("openLightbox");
  const closeBtn = document.getElementById("closeLightbox");
  if (!lightbox || !openBtn || !closeBtn) return;

  const video = lightbox.querySelector("video");

  const openLightbox = () => {
    lightbox.classList.add("active");
    if (video) {
      video.currentTime = 0;
      video.play().catch(() => {});
    }
  };

  const closeLightbox = () => {
    lightbox.classList.remove("active");
    if (video) video.pause();
  };

  openBtn.addEventListener("click", openLightbox);
  closeBtn.addEventListener("click", closeLightbox);

  lightbox.addEventListener("click", (event) => {
    if (event.target === lightbox) closeLightbox();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && lightbox.classList.contains("active")) closeLightbox();
  });
})();

document.querySelectorAll("[data-theme-toggle]").forEach((toggle) => {
	toggle.addEventListener("click", () => {
		const isLight = document.documentElement.classList.toggle("light");
		localStorage.setItem("qc-theme", isLight ? "light" : "dark");
		toggle.textContent = isLight ? "☾" : "☀";
		toggle.setAttribute("aria-label", isLight ? "Switch to dark theme" : "Switch to light theme");
	});

	const isLight = document.documentElement.classList.contains("light");
	toggle.textContent = isLight ? "☾" : "☀";
	toggle.setAttribute("aria-label", isLight ? "Switch to dark theme" : "Switch to light theme");
});


(() => {
	const form = document.getElementById("contactForm");
	if (!form) return;

	const userIdField = document.getElementById("contactUserId");
	const storedId = localStorage.getItem("quantacanvas_user_id");
	if (userIdField && storedId) userIdField.value = storedId;

	form.addEventListener("submit", async (event) => {
		event.preventDefault();
		const name = form.elements.name.value.trim();
		const email = form.elements.email.value.trim();
		const message = form.elements.message.value.trim();
		const userId = userIdField ? userIdField.value : "anonymous";

		if (!name || !email || !message) {
			showToast("Please fill in all fields.", "negative");
			return;
		}

		const submitBtn = document.getElementById("contactSubmit");
		submitBtn.disabled = true;
		submitBtn.textContent = "Sending...";

		try {
			const response = await fetch("/api/contact", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ name, email, message, user_id: userId }),
			});
			const result = await parseJsonResponse(response);
			if (!response.ok) throw new Error(result.error || "Failed to send.");
			showToast("Message sent! We'll get back to you soon.");
			form.reset();
		} catch (error) {
			showToast(error.message, "negative");
		} finally {
			submitBtn.disabled = false;
			submitBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M3 20.5 21 12 3 3.5v6.6l12.9 1.9L3 13.9v6.6Z"/></svg> Send message`;
		}
	});
})();


(() => {
	const counter = document.getElementById("genCounter");
	if (!counter) return;

	const starSvg = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2L9.19 8.63 2 9.24l5.46 4.73L5.82 21 12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61z"/></svg>`;

	
	counter.innerHTML = `${starSvg}<span>0/7</span>`;

	const uid = localStorage.getItem("quantacanvas_user_id") || "anonymous";

	fetch(`/api/generations/remaining?user_id=${encodeURIComponent(uid)}`)
		.then((r) => r.json())
		.then((data) => {
			if (typeof data.used !== "number") return;
			counter.innerHTML = `${starSvg}<span>${data.used}/${data.limit}</span>`;
			counter.title = `${data.used}/${data.limit} generations today (resets at midnight)`;
		})
		.catch(() => {});
})();