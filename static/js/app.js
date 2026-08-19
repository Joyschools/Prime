
(() => {
  const clock = document.getElementById("liveClock");
  const selectedStudentId = window.SCHOOL_PORTAL?.selectedStudentId ?? null;
  const sidebarDrawer = document.getElementById("sidebarDrawer");
  const sidebarToggle = document.getElementById("sidebarToggle");
  const sidebarClose = document.getElementById("sidebarClose");
  const scrollRail = document.getElementById("scrollRail");
  const scrollThumb = document.getElementById("scrollThumb");
  let deferredPrompt = null;
  let scrollDrag = null;

  const panels = Array.from(document.querySelectorAll(".workspace-panel"));

  function pad(n) { return String(n).padStart(2, "0"); }
  function renderClock() {
    if (!clock) return;
    const d = new Date();
    const datePart = d.toLocaleDateString(undefined, {
      weekday: "long",
      year: "numeric",
      month: "short",
      day: "numeric",
    });
    clock.textContent = `${datePart} • ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  renderClock();
  setInterval(renderClock, 1000);

  function isPhoneLayout() { return window.matchMedia && window.matchMedia("(max-width: 820px)").matches; }

  function openSidebar() {
    if (isPhoneLayout()) {
      document.body.classList.add("mobile-nav-open");
      document.body.classList.remove("sidebar-collapsed");
    } else {
      document.body.classList.remove("sidebar-collapsed");
    }
  }

  function closeSidebar() {
    if (isPhoneLayout()) {
      document.body.classList.remove("mobile-nav-open");
    } else {
      document.body.classList.add("sidebar-collapsed");
    }
  }

  function toggleSidebar() {
    if (isPhoneLayout()) {
      document.body.classList.toggle("mobile-nav-open");
      document.body.classList.remove("sidebar-collapsed");
    } else {
      document.body.classList.toggle("sidebar-collapsed");
    }
  }

  document.addEventListener("click", (event) => {
    if (!isPhoneLayout()) return;
    if (!document.body.classList.contains("mobile-nav-open")) return;
    if (event.target.closest(".sidebar") || event.target.closest(".sidebar-toggle") || event.target.closest("#prime-mobile-nav")) return;
    document.body.classList.remove("mobile-nav-open");
  });
  window.addEventListener("resize", () => {
    if (!isPhoneLayout()) document.body.classList.remove("mobile-nav-open");
  });

  function getScrollMetrics() {
    const doc = document.documentElement;
    const scrollTop = window.scrollY || doc.scrollTop || 0;
    const viewport = window.innerHeight || doc.clientHeight || 0;
    const scrollHeight = Math.max(doc.scrollHeight, document.body?.scrollHeight || 0);
    const maxScroll = Math.max(scrollHeight - viewport, 0);
    return { scrollTop, viewport, scrollHeight, maxScroll };
  }

  function syncScrollRail() {
    if (!scrollRail || !scrollThumb) return;
    const touchDevice = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
    if (touchDevice || window.innerWidth <= 820) { scrollRail.classList.remove("visible"); scrollRail.style.display = "none"; return; }
    const { scrollTop, viewport, scrollHeight, maxScroll } = getScrollMetrics();
    const scrollable = maxScroll > 4;
    if (!scrollable) {
      scrollRail.classList.remove("visible");
      scrollRail.style.display = "none";
      return;
    }
    scrollRail.style.display = "flex";
    const railHeight = scrollRail.clientHeight || Math.max(viewport - 32, 0);
    const minThumb = 34;
    const thumbHeight = Math.max(minThumb, Math.round((viewport / scrollHeight) * railHeight));
    const thumbTop = maxScroll === 0 ? 0 : Math.round((scrollTop / maxScroll) * (railHeight - thumbHeight));
    scrollThumb.style.height = `${thumbHeight}px`;
    scrollThumb.style.top = `${Math.max(0, thumbTop)}px`;
    scrollRail.classList.add("visible");
  }

  function scrollToFromRail(clientY) {
    if (!scrollRail || !scrollThumb) return;
    const { viewport, maxScroll } = getScrollMetrics();
    const rect = scrollRail.getBoundingClientRect();
    const thumbHeight = scrollThumb.offsetHeight || 34;
    const offset = Math.min(Math.max(clientY - rect.top - (thumbHeight / 2), 0), Math.max(rect.height - thumbHeight, 0));
    const ratio = rect.height - thumbHeight <= 0 ? 0 : offset / (rect.height - thumbHeight);
    window.scrollTo({ top: ratio * maxScroll, behavior: "auto" });
  }
  function showPanel(panelId, scrollTarget) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    panels.forEach((p) => p.classList.add("hidden"));
    panel.classList.remove("hidden");
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
    if (scrollTarget) {
      window.setTimeout(() => document.getElementById(scrollTarget)?.scrollIntoView({ behavior: "smooth", block: "start" }), 120);
    }
  }
  function activateFromButton(button) {
    const panelId = button.dataset.target;
    const scrollTarget = button.dataset.scroll;
    if (panelId) showPanel(panelId, scrollTarget);
  }

  sidebarToggle?.addEventListener("click", toggleSidebar);
  sidebarClose?.addEventListener("click", closeSidebar);

  if (scrollRail && scrollThumb) {
    const onPointerMove = (event) => {
      if (!scrollDrag) return;
      event.preventDefault();
      scrollToFromRail(event.clientY);
    };
    const stopDrag = () => {
      if (!scrollDrag) return;
      scrollDrag = null;
      scrollRail.classList.remove("dragging");
      syncScrollRail();
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", stopDrag);
      window.removeEventListener("pointercancel", stopDrag);
    };
    scrollThumb.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      scrollDrag = true;
      scrollRail.classList.add("dragging", "visible");
      scrollThumb.setPointerCapture?.(event.pointerId);
      scrollToFromRail(event.clientY);
      window.addEventListener("pointermove", onPointerMove, { passive: false });
      window.addEventListener("pointerup", stopDrag, { once: true });
      window.addEventListener("pointercancel", stopDrag, { once: true });
    });
    scrollRail.addEventListener("click", (event) => {
      if (event.target === scrollThumb) return;
      scrollToFromRail(event.clientY);
      syncScrollRail();
    });
    window.addEventListener("scroll", () => window.requestAnimationFrame(syncScrollRail), { passive: true });
    window.addEventListener("resize", () => window.requestAnimationFrame(syncScrollRail));
    window.addEventListener("load", syncScrollRail);
    document.addEventListener("mouseenter", syncScrollRail, true);
  }

  document.querySelectorAll(".nav-group-toggle").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      const group = button.closest(".nav-group");
      if (!group) return;

      const willOpen = !group.classList.contains("open");
      const sidebarScope = button.closest(".side-nav");
      if (sidebarScope && willOpen) {
        sidebarScope.querySelectorAll(".nav-group.open").forEach((openGroup) => {
          if (openGroup !== group) {
            openGroup.classList.remove("open");
            openGroup.querySelector(".nav-group-toggle")?.setAttribute("aria-expanded", "false");
          }
        });
      }

      group.classList.toggle("open", willOpen);
      button.setAttribute("aria-expanded", String(willOpen));
    });
  });

  document.querySelectorAll(".nav-action").forEach((button) => {
    button.addEventListener("click", () => activateFromButton(button));
  });

  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferredPrompt = e;
  });

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", async () => {
      try { await navigator.serviceWorker.register("/sw.js"); } catch (err) { console.warn("SW registration failed", err); }
    });
  }

  const modal = document.getElementById("studentModal");
  const closeBtn = document.getElementById("closeStudentModal");
  const openBtn = document.getElementById("openStudentBtn");
  const modalEls = {
    name: document.getElementById("modalStudentName"),
    admission: document.getElementById("modalAdmission"),
    grade: document.getElementById("modalGrade"),
    status: document.getElementById("modalStatus"),
    balance: document.getElementById("modalBalance"),
    guardian: document.getElementById("modalGuardian"),
    guardianPhone: document.getElementById("modalGuardianPhone"),
    guardianEmail: document.getElementById("modalGuardianEmail"),
    medical: document.getElementById("modalMedical"),
    allergies: document.getElementById("modalAllergies"),
    specialInfo: document.getElementById("modalSpecialInfo"),
    paymentsBody: document.getElementById("modalPaymentsBody"),
    editForm: document.getElementById("studentEditForm"),
    editAdmissionNo: document.getElementById("editAdmissionNo"),
    editFullName: document.getElementById("editFullName"),
    editGrade: document.getElementById("editGrade"),
    editPaymentStatus: document.getElementById("editPaymentStatus"),
    editBalance: document.getElementById("editBalance"),
    editActive: document.getElementById("editActive"),
    editGuardianName: document.getElementById("editGuardianName"),
    editGuardianPhone: document.getElementById("editGuardianPhone"),
    editGuardianEmail: document.getElementById("editGuardianEmail"),
    editMedical: document.getElementById("editMedical"),
    editAllergies: document.getElementById("editAllergies"),
    editSpecialInfo: document.getElementById("editSpecialInfo"),
  };

  function renderPaymentRows(payments = []) {
    if (!modalEls.paymentsBody) return;
    modalEls.paymentsBody.innerHTML = payments.length
      ? payments.map(p => `
        <tr>
          <td>${(p.created_at || "").slice(0, 16)}</td>
          <td>KES ${Number(p.amount || 0).toLocaleString()}</td>
          <td>${p.method || ""}</td>
          <td>${p.recorded_by_name || ""}</td>
          <td><span class="pill ${String(p.status || "").toLowerCase()}">${p.status || ""}</span></td>
        </tr>
      `).join("")
      : `<tr><td colspan="5" class="muted">No payments found.</td></tr>`;
  }

  async function loadStudent(studentId) {
    if (!studentId) return false;
    try {
      const res = await fetch(`/api/student/${studentId}`);
      if (!res.ok) throw new Error("Failed to load student");
      const data = await res.json();
      const s = data.student;
      modalEls.name.textContent = s.full_name || "Student";
      modalEls.admission.textContent = s.admission_no || "";
      modalEls.grade.textContent = s.grade || "";
      modalEls.status.textContent = s.payment_status || "";
      modalEls.balance.textContent = "KES " + Number(s.balance || 0).toLocaleString();
      modalEls.guardian.textContent = s.guardian_name || "—";
      modalEls.guardianPhone.textContent = s.guardian_phone || "—";
      modalEls.guardianEmail.textContent = s.guardian_email || "—";
      modalEls.medical.textContent = s.medical_condition || "—";
      modalEls.allergies.textContent = s.allergies || "—";
      modalEls.specialInfo.textContent = s.special_info || "—";
      if (modalEls.editForm) modalEls.editForm.action = `/students/${studentId}/update`;
      if (modalEls.editAdmissionNo) modalEls.editAdmissionNo.value = s.admission_no || "";
      if (modalEls.editFullName) modalEls.editFullName.value = s.full_name || "";
      if (modalEls.editGrade) modalEls.editGrade.value = s.grade || "";
      if (modalEls.editPaymentStatus) modalEls.editPaymentStatus.value = s.payment_status || "Pending";
      if (modalEls.editBalance) modalEls.editBalance.value = Number(s.balance || 0);
      if (modalEls.editActive) modalEls.editActive.value = String(Number(s.active ?? 1));
      if (modalEls.editGuardianName) modalEls.editGuardianName.value = s.guardian_name || "";
      if (modalEls.editGuardianPhone) modalEls.editGuardianPhone.value = s.guardian_phone || "";
      if (modalEls.editGuardianEmail) modalEls.editGuardianEmail.value = s.guardian_email || "";
      if (modalEls.editMedical) modalEls.editMedical.value = s.medical_condition || "";
      if (modalEls.editAllergies) modalEls.editAllergies.value = s.allergies || "";
      if (modalEls.editSpecialInfo) modalEls.editSpecialInfo.value = s.special_info || "";
      renderPaymentRows(data.payments || []);
      if (modal && typeof modal.showModal === "function") {
        try { modal.showModal(); } catch (_) { window.location.href = `/dashboard?student_id=${studentId}#students-panel`; }
      } else {
        window.location.href = `/dashboard?student_id=${studentId}#students-panel`;
      }
      return true;
    } catch (err) {
      console.warn(err);
      const fallback = `/dashboard?student_id=${studentId}#students-panel`;
      window.location.href = fallback;
      return false;
    }
  }

  document.querySelectorAll(".js-view-student").forEach(btn => btn.addEventListener("click", (e) => {
    e.preventDefault?.();
    loadStudent(btn.dataset.studentId);
  }));
  document.querySelectorAll(".student-row").forEach(row => row.addEventListener("click", (e) => {
    if (e.target.closest("button, form, a")) return;
    loadStudent(row.dataset.studentId);
  }));
  openBtn?.addEventListener("click", () => { if (selectedStudentId) loadStudent(selectedStudentId); });
  if (selectedStudentId) {
    window.setTimeout(() => loadStudent(selectedStudentId), 50);
  }
  closeBtn?.addEventListener("click", () => modal?.close());
  modal?.addEventListener("click", (e) => {
    const rect = modal.querySelector(".modal-card")?.getBoundingClientRect();
    if (!rect) return;
    const inDialog = e.clientX >= rect.left && e.clientX <= rect.right && e.clientY >= rect.top && e.clientY <= rect.bottom;
    if (!inDialog) modal.close();
  });

  document.querySelectorAll("form").forEach(form => {
    form.addEventListener("submit", () => {
      const btn = form.querySelector("button[type='submit']");
      if (btn) {
        btn.disabled = true;
        setTimeout(() => btn.disabled = false, 3000);
      }
    });
  });

  if (panels.length) {
    const defaultPanel = document.querySelector(".workspace-panel:not(.hidden)") || document.getElementById("home-panel") || panels[0];
    if (defaultPanel) panels.forEach((p) => p !== defaultPanel && p.classList.add("hidden"));
  }
  syncScrollRail();
})();
