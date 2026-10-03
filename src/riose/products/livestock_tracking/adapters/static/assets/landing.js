    const hero = document.querySelector('.hero');
    const heroCopy = document.getElementById('hero-copy');
    const heroSubtitle = document.getElementById('hero-subtitle');
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let heroCopyFrame = 0;

    const updateHeroCopy = () => {
      heroCopyFrame = 0;
      const scrollRange = Math.max(1, window.innerHeight * 0.15);
      const progress = Math.max(0, Math.min(1, window.scrollY / scrollRange));
      const interacting = hero.classList.contains('is-interacting');
      const interactionOpacity = interacting ? 0.58 : 1;
      const subtitleProgress = Math.max(0, Math.min(1, progress / 0.82));
      heroCopy.style.setProperty('--hero-copy-opacity', String((1 - progress) * interactionOpacity));
      heroCopy.style.setProperty('--hero-copy-shift', `${reducedMotion ? 0 : -40 * progress}px`);
      heroSubtitle.style.setProperty('--hero-subtitle-opacity', String((1 - subtitleProgress) * interactionOpacity));
      heroSubtitle.style.setProperty('--hero-subtitle-shift', `${reducedMotion ? 0 : -20 * subtitleProgress}px`);
    };

    const scheduleHeroCopyUpdate = () => {
      if (!heroCopyFrame) heroCopyFrame = requestAnimationFrame(updateHeroCopy);
    };
    scheduleHeroCopyUpdate();
    window.addEventListener('resize', scheduleHeroCopyUpdate, { passive: true });
    window.addEventListener('scroll', scheduleHeroCopyUpdate, { passive: true });
    window.addEventListener('riose:hero-interaction', scheduleHeroCopyUpdate);

    const header = document.getElementById('floating-header');
    const updateHeader = () => header.classList.toggle('is-scrolled', window.scrollY > 72);
    updateHeader();
    window.addEventListener('scroll', updateHeader, { passive: true });

    const brandMeaning = document.querySelector('.brand-meaning');
    const brandTrigger = brandMeaning.querySelector('.wordmark-trigger');
    const brandPanel = brandMeaning.querySelector('.brand-definition');
    const reducedBrandMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const fineHover = window.matchMedia('(hover: hover) and (pointer: fine)').matches;
    let fadeTimer;
    let shrinkTimer;

    const clearBrandTimers = () => {
      window.clearTimeout(fadeTimer);
      window.clearTimeout(shrinkTimer);
    };

    const openBrandDefinition = () => {
      clearBrandTimers();
      brandMeaning.classList.remove('is-closing', 'is-fading');
      brandMeaning.classList.add('is-open');
      brandTrigger.setAttribute('aria-expanded', 'true');
      brandPanel.setAttribute('aria-hidden', 'false');
    };

    const closeBrandDefinition = () => {
      clearBrandTimers();
      brandTrigger.setAttribute('aria-expanded', 'false');
      brandPanel.setAttribute('aria-hidden', 'true');
      if (reducedBrandMotion) {
        brandMeaning.classList.remove('is-open', 'is-closing', 'is-fading');
        return;
      }
      if (!brandMeaning.classList.contains('is-open')) return;
      brandMeaning.classList.add('is-fading');
      fadeTimer = window.setTimeout(() => {
        brandMeaning.classList.remove('is-open');
        brandMeaning.classList.add('is-closing');
        shrinkTimer = window.setTimeout(() => {
          brandMeaning.classList.remove('is-closing', 'is-fading');
        }, 680);
      }, 150);
    };

    brandMeaning.addEventListener('pointerenter', event => {
      if (event.pointerType === 'mouse') openBrandDefinition();
    });
    brandMeaning.addEventListener('pointerleave', event => {
      if (event.pointerType === 'mouse' && !brandTrigger.matches(':focus-visible')) closeBrandDefinition();
    });
    brandMeaning.addEventListener('pointermove', event => {
      if (event.pointerType !== 'mouse' || !brandMeaning.classList.contains('is-open')) return;
      const bounds = brandPanel.getBoundingClientRect();
      if (event.clientY < bounds.top || event.clientY > bounds.bottom) return;
      const x = Math.max(0, Math.min(100, ((event.clientX - bounds.left) / bounds.width) * 100));
      const y = Math.max(0, Math.min(100, ((event.clientY - bounds.top) / bounds.height) * 100));
      brandPanel.style.setProperty('--shine-x', `${x}%`);
      brandPanel.style.setProperty('--shine-y', `${y}%`);
    });
    brandMeaning.addEventListener('focusin', () => {
      if (fineHover) openBrandDefinition();
    });
    brandMeaning.addEventListener('focusout', event => {
      if (!brandMeaning.contains(event.relatedTarget)) closeBrandDefinition();
    });
    brandTrigger.addEventListener('click', event => {
      if (fineHover && event.detail > 0) return;
      if (brandMeaning.classList.contains('is-open') && !brandMeaning.classList.contains('is-closing')) closeBrandDefinition();
      else openBrandDefinition();
    });
    brandTrigger.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      closeBrandDefinition();
      brandTrigger.blur();
    });
    document.addEventListener('pointerdown', event => {
      if (event.pointerType === 'touch' && !brandMeaning.contains(event.target)) closeBrandDefinition();
    });

    const sceneWrap = document.getElementById('scene-wrap');
    const sceneObserver = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return;
      const sceneScript = document.createElement('script');
      sceneScript.type = 'module';
      sceneScript.src = '/assets/product-scene.js?v=20261003-6';
      sceneScript.onerror = () => {
        sceneWrap.classList.add('is-fallback');
        document.getElementById('scene-status').textContent = 'Interactive model unavailable. Showing the product photograph.';
      };
      document.head.append(sceneScript);
      sceneObserver.disconnect();
    }, { rootMargin: '120px 0px' });
    sceneObserver.observe(sceneWrap);
