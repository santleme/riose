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
        shrinkTimer = window.setTimeout(() => brandMeaning.classList.remove('is-closing', 'is-fading'), 680);
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
      brandPanel.style.setProperty('--shine-x', `${Math.max(0, Math.min(100, ((event.clientX - bounds.left) / bounds.width) * 100))}%`);
      brandPanel.style.setProperty('--shine-y', `${Math.max(0, Math.min(100, ((event.clientY - bounds.top) / bounds.height) * 100))}%`);
    });
    brandMeaning.addEventListener('focusin', () => { if (fineHover) openBrandDefinition(); });
    brandMeaning.addEventListener('focusout', event => { if (!brandMeaning.contains(event.relatedTarget)) closeBrandDefinition(); });
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

    const architecture = document.getElementById('ascii-structure');
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const rowCount = 124;
    const columnCount = 112;
    const midline = 64;
    const grid = Array.from({ length: rowCount }, () => Array(columnCount).fill(' '));
    const put = (row, column, value) => {
      if (row >= 0 && row < rowCount && column >= 0 && column < columnCount) grid[row][column] = value;
    };
    const beam = (row, left, right, glyph = '─') => {
      for (let column = left; column <= right; column += 1) put(row, column, glyph);
    };
    const write = (row, column, text) => [...text].forEach((glyph, offset) => put(row, column + offset, glyph));
    const halfWidthAt = row => row < 13 ? 2 : row < 24 ? 7 : row < 42 ? 16 : row < 73 ? 22 : row < 96 ? 30 : 42;

    for (let row = 0; row < rowCount; row += 1) {
      const center = midline + Math.round(Math.sin(row / 17) * 3);
      const halfWidth = halfWidthAt(row);
      let left = center - halfWidth;
      let right = center + halfWidth;

      if ((row >= 23 && row <= 27) || (row >= 61 && row <= 65)) {
        left -= 12;
        right += 12;
      }

      put(row, center, row % 10 === 0 ? '┬' : '│');
      put(row, center - 3, '│');
      put(row, center + 3, '│');
      if (row % 19 === 0) put(row, center, '+');
      if (row >= 84 && row % 12 < 2) put(row, center, '█');
      if (row >= 35 && row <= 44 && row % 3 === 0) {
        put(row, center - 1, ':');
        put(row, center + 1, ':');
      }
      put(row, left, row % 8 === 0 ? '├' : (row % 2 === 0 ? '╱' : '│'));
      put(row, right, row % 8 === 0 ? '┤' : (row % 2 === 0 ? '╲' : '│'));

      if (row % 8 === 0) {
        beam(row, left + 1, right - 1);
        put(row, center, '┴');
        put(row, center - 3, '┬');
        put(row, center + 3, '┬');
      } else {
        const braceLeft = left + Math.max(2, Math.floor(halfWidth * .34));
        const braceRight = right - Math.max(2, Math.floor(halfWidth * .34));
        put(row, braceLeft, row % 2 === 0 ? '╲' : '╱');
        put(row, braceRight, row % 2 === 0 ? '╱' : '╲');
        if (row % 5 === 0) {
          put(row, center - 1, '░');
          put(row, center + 1, '▒');
        }
      }

      if (row >= 3 && row <= 12) {
        if (row === 5 || row === 10) {
          for (let offset = 5; offset <= 13; offset += 1) {
            put(row, center - offset, row % 2 ? '·' : '─');
            put(row, center + offset, row % 2 ? '·' : '─');
          }
          put(row, center - 4, '┬');
          put(row, center + 4, '┬');
        }
        if (row === 8) {
          for (let offset = 6; offset <= 12; offset += 1) {
            put(row, center - offset, '·');
            put(row, center + offset, '·');
          }
        }
      }

      if ([18, 31, 47, 56, 70, 88, 105, 117].includes(row)) {
        const mark = row % 2 ? '┬' : '┴';
        for (let offset = -halfWidth; offset <= halfWidth; offset += 1) {
          if (offset % 5 === 0) put(row, center + offset, mark);
        }
      }

      if (row >= 76 && row <= 96 && row % 4 === 1) {
        for (let offset = -halfWidth + 3; offset < halfWidth - 2; offset += 6) {
          put(row, center + offset, row % 8 === 1 ? '░' : '#');
        }
      }

      if (row >= 99) {
        const foot = Math.floor((row - 99) / 3);
        put(row, center - halfWidth + foot, row % 2 ? '╱' : '▒');
        put(row, center + halfWidth - foot, row % 2 ? '╲' : '▒');
      }
    }

    const traceColumn = (row, offset, text) => write(row, midline + offset, text);
    traceColumn(20, 11, 'RSSI');
    traceColumn(34, -15, 'dBm');
    traceColumn(49, 14, '5.18 GHz');
    traceColumn(63, -18, 'TX    RX');
    traceColumn(81, 20, 'SIGNAL');
    traceColumn(98, -28, 'FIELD');
    traceColumn(111, 8, 'RF');

    const architectureLines = grid.map(row => row.join('').replace(/\s+$/, ''));
    const lineNodes = architectureLines.map((text, index) => {
      const line = document.createElement('span');
      const density = [...text].filter(character => character !== ' ').length / columnCount;
      line.className = 'architecture-line';
      line.dataset.finalOpacity = String(Math.min(.88, .42 + density * .78));
      line.style.setProperty('--line-opacity', line.dataset.finalOpacity);
      line.style.setProperty('--line-delay', `${index * 11}ms`);
      const tracePattern = /(5\.18 GHz|SIGNAL|FIELD|RSSI|dBm|RF|TX|RX)/g;
      let start = 0;
      for (const match of text.matchAll(tracePattern)) {
        line.append(document.createTextNode(text.slice(start, match.index)));
        const fragment = document.createElement('span');
        fragment.className = 'signal-trace';
        fragment.textContent = match[0];
        line.append(fragment);
        start = match.index + match[0].length;
      }
      line.append(document.createTextNode(text.slice(start)));
      return line;
    });
    architecture.replaceChildren(...lineNodes);

    if (reducedMotion) architecture.classList.remove('is-building');
    else {
      lineNodes[lineNodes.length - 1].addEventListener('animationend', () => {
        architecture.classList.remove('is-building');
        updateStructure();
      }, { once: true });
    }

    let pointerX = 0;
    let pointerY = 0;
    let framePending = false;
    const updateStructure = () => {
      const scrollDrift = Math.min(7, window.scrollY * .004);
      architecture.style.setProperty('--parallax-x', `${pointerX}px`);
      architecture.style.setProperty('--parallax-y', `${pointerY + scrollDrift}px`);
      if (!architecture.classList.contains('is-building')) {
        const viewportCenter = window.scrollY + window.innerHeight * .52;
        for (const line of lineNodes) {
          const bounds = line.getBoundingClientRect();
          const documentY = bounds.top + window.scrollY + bounds.height / 2;
          const distance = Math.abs(documentY - viewportCenter) / Math.max(window.innerHeight, 1);
          const signalPass = Math.max(0, 1 - distance * 1.7);
          const exposure = .48 + signalPass * .3;
          line.style.opacity = String(Number(line.dataset.finalOpacity) * exposure);
        }
      }
      framePending = false;
    };
    const requestStructureUpdate = () => {
      if (reducedMotion || framePending) return;
      framePending = true;
      window.requestAnimationFrame(updateStructure);
    };

    window.addEventListener('pointermove', event => {
      if (reducedMotion || event.pointerType !== 'mouse') return;
      pointerX = ((event.clientX / window.innerWidth) - .5) * 8;
      pointerY = ((event.clientY / window.innerHeight) - .5) * 6;
      requestStructureUpdate();
    }, { passive: true });
    window.addEventListener('scroll', requestStructureUpdate, { passive: true });
    window.addEventListener('resize', requestStructureUpdate, { passive: true });
    requestStructureUpdate();
