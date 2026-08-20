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

	 promptInput.addEventListener("input", () => {
		 sendPrompt.disabled = !promptInput.value.trim();
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
		 } else if (loadingMessageTimer) {
			 clearInterval(loadingMessageTimer);
			 loadingMessageTimer = null;
		 }
	};

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

	const showWorkspace = (result, prompt) => {
		const id = crypto.randomUUID();
		sessionStorage.setItem(`qc-viz-${id}`, result.html);
		const params = new URLSearchParams({
			id,
			prompt,
			summary: result.summary || "",
		});
		window.location.assign(`/responses?${params.toString()}`);
	};

	promptForm.addEventListener("submit", async (event) => {
		event.preventDefault();
		const prompt = promptInput.value.trim();
		if (!prompt) return;
		requestController = new AbortController();
		setLoading(true);
		showToast("The model is cooking up an explanation for you. This may take a few minutes.");
		try {
			const response = await fetch("/api/generate", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ prompt, user_id: userId() }),
				signal: requestController.signal,
			});
			const result = await response.json();
			if (!response.ok) throw new Error(result.error || "Generation failed.");
			history = [];
			showWorkspace(result, prompt);
		} catch (error) {
			if (error.name !== "AbortError") {
				showToast(error.message, "negative");
			} else {
				showToast("Generation cancelled.");
			}
		} finally {
			requestController = null;
			setLoading(false);
		}
	});

	abortButton.addEventListener("click", () => requestController?.abort());
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
