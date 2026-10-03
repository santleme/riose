const status = document.getElementById('scene-status');
const wrapper = document.getElementById('scene-wrap');
const canvas = document.getElementById('product-canvas');
const fallback = document.querySelector('.scene-fallback');

let THREE;
try {
  THREE = await import('./vendor/three.module.js');
} catch (error) {
  console.warn('RIOSE 3D scene is unavailable; showing the product photograph.', error);
  wrapper.classList.add('is-fallback');
  status.textContent = 'Interactive model unavailable. Showing the product photograph.';
}

if (THREE) startScene(THREE);

function startScene(THREE) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, alpha: false, antialias: true, powerPreference: 'high-performance' });
  } catch (error) {
    console.warn('WebGL could not initialize; showing the product photograph.', error);
    wrapper.classList.add('is-fallback');
    status.textContent = 'Interactive model unavailable. Showing the product photograph.';
    return;
  }

  const paperColor = 0xf2f2f0;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(paperColor);
  scene.environment = makeStudioEnvironment(THREE);
  scene.environmentIntensity = 0.52;
  const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 30);
  camera.position.set(0, 0, 6.7);

  const pixelRatioCap = window.matchMedia('(max-width: 640px)').matches ? 1.12 : 1.35;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, pixelRatioCap));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.12;
  // The soft contact shadow below is cheaper than a shadow map and avoids a
  // costly first-frame shadow pass on phones and integrated graphics.
  renderer.shadowMap.enabled = false;
  renderer.setClearColor(paperColor, 1);

  scene.add(new THREE.HemisphereLight(0xffffff, 0xc7c4b9, 0.9));
  const key = new THREE.DirectionalLight(0xfff8e8, 2.1);
  key.position.set(-3.4, 4.5, 5);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.48);
  fill.position.set(4.5, 1.4, 3.2);
  scene.add(fill);
  const rim = new THREE.DirectionalLight(0xffffff, 0.95);
  rim.position.set(0.5, 2.5, -4);
  scene.add(rim);

  const product = new THREE.Group();
  scene.add(product);

  const mouldTexture = makeMouldTexture(THREE);
  const mouldAlbedo = makeMouldAlbedoTexture(THREE);
  const shellMaterials = [];
  const yellow = makeInspectionShellMaterial(THREE, {
    inspectionFrontZ: 0.06,
    color: 0xffe11c,
    map: mouldAlbedo,
    roughness: 0.72,
    metalness: 0.015,
    bumpMap: mouldTexture,
    bumpScale: 0.012,
    clearcoat: 0.15,
    clearcoatRoughness: 0.56,
  });
  shellMaterials.push(yellow);
  const yellowRim = makeInspectionShellMaterial(THREE, {
    inspectionFrontZ: 0.055,
    color: 0xf6cd12,
    map: mouldAlbedo,
    roughness: 0.75,
    metalness: 0.01,
    bumpMap: mouldTexture,
    bumpScale: 0.01,
    clearcoat: 0.1,
    clearcoatRoughness: 0.66,
  });
  shellMaterials.push(yellowRim);
  const yellowHardware = new THREE.MeshStandardMaterial({
    color: 0xf3c900,
    roughness: 0.62,
    metalness: 0.02,
    bumpMap: mouldTexture,
    bumpScale: 0.0015,
  });
  const blackPolymer = new THREE.MeshStandardMaterial({
    color: 0x171817,
    roughness: 0.86,
    metalness: 0.015,
    bumpMap: mouldTexture,
    bumpScale: 0.006,
  });

  const tagShape = makeTagShape(THREE);
  const bodyGeometry = new THREE.ExtrudeGeometry(tagShape, {
    depth: 0.12,
    bevelEnabled: true,
    bevelSegments: 6,
    bevelSize: 0.019,
    bevelThickness: 0.019,
    curveSegments: 32,
  });
  const body = new THREE.Mesh(bodyGeometry, yellow);
  body.position.z = -0.06;
  body.castShadow = true;
  body.receiveShadow = true;
  product.add(body);

  const raisedShape = makeTagShape(THREE, 0.982);
  const raisedGeometry = makeDomedFaceGeometry(THREE, raisedShape, 0.063, 0.02);
  const raisedFace = new THREE.Mesh(raisedGeometry, yellowRim);
  product.add(raisedFace);

  // A shallow molded bead follows the visible right shoulder and lower edge,
  // matching the raised perimeter in the supplied tag photo without changing
  // its broad, squared shield silhouette.
  const perimeterCurve = new THREE.CatmullRomCurve3([
    new THREE.Vector3(0.255, 1.11, 0.085),
    new THREE.Vector3(0.33, 0.96, 0.085),
    new THREE.Vector3(0.34, 0.75, 0.085),
    new THREE.Vector3(0.46, 0.49, 0.083),
    new THREE.Vector3(0.73, 0.29, 0.08),
    new THREE.Vector3(0.95, 0.12, 0.078),
    new THREE.Vector3(0.995, -0.12, 0.077),
    new THREE.Vector3(0.995, -1.08, 0.077),
    new THREE.Vector3(0.94, -1.22, 0.077),
    new THREE.Vector3(0.76, -1.285, 0.077),
    new THREE.Vector3(0.18, -1.30, 0.077),
  ]);
  product.add(new THREE.Mesh(new THREE.TubeGeometry(perimeterCurve, 84, 0.014, 6, false), yellowRim));

  // Two-piece attachment: front pin head, rear post and circular backing plate.
  const connectorScale = 0.9;
  addCylinderZ(THREE, product, yellowHardware, 0.272, 0.292, 0.1, 0.105, 0.86);
  addCylinderZ(THREE, product, blackPolymer, 0.183 * connectorScale, 0.205 * connectorScale, 0.09, 0.19, 0.86);
  addCylinderZ(THREE, product, blackPolymer, 0.144 * connectorScale, 0.148 * connectorScale, 0.012, 0.24, 0.86);
  const connectorRim = new THREE.Mesh(new THREE.TorusGeometry(0.123 * connectorScale, 0.008 * connectorScale, 8, 48), blackPolymer);
  connectorRim.position.set(0, 0.86, 0.249);
  product.add(connectorRim);
  const connectorFace = new THREE.Mesh(
    new THREE.CircleGeometry(0.144 * connectorScale, 64),
    new THREE.MeshStandardMaterial({
      map: makeConnectorFaceTexture(THREE),
      roughness: 0.92,
      metalness: 0.025,
      bumpMap: mouldTexture,
      bumpScale: 0.008,
    }),
  );
  connectorFace.position.set(0, 0.86, 0.247);
  product.add(connectorFace);
  const connectorBore = new THREE.Mesh(new THREE.CircleGeometry(0.025 * connectorScale, 32), new THREE.MeshStandardMaterial({ color: 0x080908, roughness: 0.95 }));
  connectorBore.position.set(0, 0.86, 0.251);
  product.add(connectorBore);

  const rearPin = new THREE.Group();
  product.add(rearPin);
  addCylinderZ(THREE, rearPin, blackPolymer, 0.13, 0.13, 0.56, -0.35, 0.86);
  addCylinderZ(THREE, rearPin, yellowHardware, 0.285, 0.3, 0.12, -0.59, 0.86);
  addCylinderZ(THREE, rearPin, yellowHardware, 0.105, 0.105, 0.16, -0.38, 0.86);
  const pinPoint = new THREE.Mesh(new THREE.ConeGeometry(0.13, 0.22, 40), blackPolymer);
  pinPoint.rotation.x = -Math.PI / 2;
  pinPoint.position.set(0, 0.86, -0.82);
  rearPin.add(pinPoint);
  addCylinderZ(THREE, rearPin, blackPolymer, 0.19, 0.19, 0.06, -0.68, 0.86);

  const inspectionWindow = new THREE.Mesh(
    new THREE.ExtrudeGeometry(makeLowerWindowShape(THREE), {
      depth: 0.025,
      bevelEnabled: true,
      bevelSegments: 3,
      bevelSize: 0.012,
      bevelThickness: 0.01,
      curveSegments: 16,
    }),
    new THREE.MeshPhysicalMaterial({
      color: 0x777a72,
      roughness: 0.4,
      metalness: 0.015,
      clearcoat: 0.62,
      clearcoatRoughness: 0.28,
      transparent: true,
      opacity: 0,
      depthWrite: false,
      side: THREE.DoubleSide,
    }),
  );
  inspectionWindow.position.z = 0.072;
  inspectionWindow.visible = false;
  product.add(inspectionWindow);

  // Interior is a visual study of the visible board, ICs and copper traces.
  // No battery or unreferenced power subsystem is included.
  const internals = new THREE.Group();
  internals.visible = false;
  internals.position.z = -0.055;
  product.add(internals);
  let hasBuiltInterior = false;
  const ensureInterior = () => {
    if (hasBuiltInterior) return;
    buildInterior(THREE, internals);
    hasBuiltInterior = true;
  };
  const warmInteriorWhenIdle = () => {
    const build = () => {
      if (!hasBuiltInterior) ensureInterior();
    };
    if ('requestIdleCallback' in window) window.requestIdleCallback(build, { timeout: 1200 });
    else window.setTimeout(build, 320);
  };

  const wordmark = makeWordmarkTexture(THREE);
  const markX = 0.29;
  const markY = -1.015;
  const mark = new THREE.Mesh(
    makeDomedDecalGeometry(THREE, 0.84, 0.18, markX, markY, 0.065, 0.046),
    new THREE.MeshBasicMaterial({ map: wordmark, transparent: true, depthWrite: false, toneMapped: false }),
  );
  mark.position.set(markX, markY, 0);
  product.add(mark);

  const ambientShadow = new THREE.Mesh(
    new THREE.PlaneGeometry(2.25, 0.72),
    new THREE.MeshBasicMaterial({ map: makeShadowTexture(THREE), transparent: true, depthWrite: false, opacity: 0.34 }),
  );
  ambientShadow.rotation.x = 0;
  ambientShadow.position.set(0.08, -1.32, -0.88);
  scene.add(ambientShadow);

  const motion = {
    yaw: -0.5,
    pitch: 0.12,
    velocityX: 0,
    velocityY: 0,
    targetInspection: 0,
    inspection: 0,
    inspectionFrom: 0,
    inspectionStartedAt: performance.now(),
    inspectionDuration: 700,
    inspectTimer: 0,
    dragging: false,
    pointers: new Map(),
    lastPointer: null,
    hoverPoint: null,
    hoverDirty: false,
    zoom: 6.7,
    lastTime: performance.now(),
    lastPointerTime: 0,
  };
  product.rotation.order = 'YXZ';
  product.rotation.y = motion.yaw;
  product.rotation.x = motion.pitch;

  const minZoom = 4.7;
  const maxZoom = 7.1;
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const resize = () => {
    const bounds = wrapper.getBoundingClientRect();
    const width = Math.max(1, bounds.width);
    const height = Math.max(1, bounds.height);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.fov = width < 640 ? 37 : 32;
    camera.updateProjectionMatrix();
    product.scale.setScalar(width < 640 ? 0.82 : 0.91);
    scheduleFrame(true);
  };

  let frameHandle = 0;
  let isVisible = true;
  let hasRendered = false;
  let renderFailed = false;
  let animate = () => {};
  const isMotionActive = () => motion.dragging || motion.pointers.size > 0
    || Math.abs(motion.velocityX) > 0.0002 || Math.abs(motion.velocityY) > 0.0002
    || Math.abs(motion.targetInspection - motion.inspection) > 0.001
    || Math.abs(motion.zoom - camera.position.z) > 0.003
    || motion.hoverDirty;
  const scheduleFrame = (force = false) => {
    if (renderFailed || (!force && !isMotionActive()) || !isVisible || document.hidden || frameHandle) return;
    frameHandle = requestAnimationFrame((timestamp) => animate(timestamp));
  };
  resize();
  const observer = new ResizeObserver(resize);
  observer.observe(wrapper);

  const raycaster = new THREE.Raycaster();
  const pointerNdc = new THREE.Vector2();
  const inspectableObjects = product.children.filter((child) => child !== internals && child !== inspectionWindow);

  const isOverProduct = (event) => {
    const bounds = canvas.getBoundingClientRect();
    const relativeX = (event.clientX - bounds.left) / bounds.width;
    const relativeY = (event.clientY - bounds.top) / bounds.height;
    if (Math.abs(relativeX - 0.5) > 0.38 || Math.abs(relativeY - 0.5) > 0.4) return false;
    pointerNdc.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
    pointerNdc.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
    camera.updateMatrixWorld(true);
    product.updateMatrixWorld(true);
    raycaster.setFromCamera(pointerNdc, camera);
    return raycaster.intersectObjects(inspectableObjects, true).length > 0;
  };

  const setProductHover = (isHovering) => {
    wrapper.classList.toggle('is-over-product', isHovering);
  };

  const setInspectTarget = (value) => {
    const next = value ? 1 : 0;
    if (next) ensureInterior();
    if (motion.targetInspection === next) return;
    motion.targetInspection = next;
    motion.inspectionFrom = motion.inspection;
    motion.inspectionStartedAt = performance.now();
    internals.visible = next > 0 || motion.inspection > 0.015;
    status.textContent = next
      ? 'Illustrative internal study shown. Its component layout is not verified from the available product references.'
      : 'Exterior product view.';
    scheduleFrame();
  };

  const clearInspectTimer = () => {
    window.clearTimeout(motion.inspectTimer);
    motion.inspectTimer = 0;
  };

  const scheduleHoverInspect = () => {
    if (motion.dragging || motion.pointers.size > 0 || motion.targetInspection > 0) return;
    if (motion.inspectTimer) return;
    motion.inspectTimer = window.setTimeout(() => {
      motion.inspectTimer = 0;
      setInspectTarget(1);
    }, prefersReducedMotion ? 0 : 300);
  };

  const zoomBy = (amount) => {
    motion.zoom = THREE.MathUtils.clamp(motion.zoom + amount, minZoom, maxZoom);
    scheduleFrame();
  };

  const pointerDistance = () => {
    const points = [...motion.pointers.values()];
    if (points.length < 2) return 0;
    return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
  };
  let pinchStart = 0;
  let pinchZoom = motion.zoom;

  wrapper.addEventListener('pointerdown', (event) => {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    if (event.pointerType === 'mouse') {
      // The wrapper is keyboard-focusable; prevent the browser from focusing
      // it on a click in the empty hero around the product.
      event.preventDefault();
      window.getSelection()?.removeAllRanges();
    }
    // The hero fills the viewport, but only the physical tag should capture a
    // drag. Otherwise a blank-space click steals focus and page-level keys.
    const isPinchContinuation = event.pointerType === 'touch' && motion.pointers.size === 1;
    const startedOnProduct = isOverProduct(event);
    if (!startedOnProduct && !isPinchContinuation) {
      if (document.activeElement === wrapper) wrapper.blur();
      return;
    }
    try {
      wrapper.setPointerCapture(event.pointerId);
    } catch {
      return;
    }
    // preventDefault stops the browser's normal selection cleanup too.
    // Explicitly clear a stale text selection before the product drag begins.
    window.getSelection()?.removeAllRanges();
    motion.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    document.body.classList.add('is-product-dragging');
    clearInspectTimer();
    motion.dragging = motion.pointers.size === 1;
    if (startedOnProduct) {
      setProductHover(true);
      setInspectTarget(1);
    }
    if (motion.pointers.size === 2) {
      motion.dragging = false;
      pinchStart = pointerDistance();
      pinchZoom = motion.zoom;
    }
    motion.lastPointer = { x: event.clientX, y: event.clientY };
    motion.lastPointerTime = event.timeStamp;
    scheduleFrame();
  });

  wrapper.addEventListener('pointermove', (event) => {
    if (motion.pointers.has(event.pointerId)) {
      motion.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (motion.pointers.size >= 2) {
        const distance = pointerDistance();
        if (pinchStart > 0) motion.zoom = THREE.MathUtils.clamp(pinchZoom + (pinchStart - distance) * 0.008, minZoom, maxZoom);
        scheduleFrame();
        return;
      }
      if (!motion.dragging || !motion.lastPointer) return;
      const dx = event.clientX - motion.lastPointer.x;
      const dy = event.clientY - motion.lastPointer.y;
      const bounds = wrapper.getBoundingClientRect();
      const moveSeconds = Math.max(0.008, (event.timeStamp - motion.lastPointerTime) / 1000);
      const deltaYaw = dx / Math.max(1, bounds.width) * 4.3;
      const deltaPitch = dy / Math.max(1, bounds.height) * 3.1;
      motion.velocityX = deltaYaw / moveSeconds;
      motion.velocityY = deltaPitch / moveSeconds;
      motion.yaw += deltaYaw;
      motion.pitch = THREE.MathUtils.clamp(motion.pitch + deltaPitch, -1.12, 1.12);
      motion.lastPointer = { x: event.clientX, y: event.clientY };
      motion.lastPointerTime = event.timeStamp;
      scheduleFrame();
      return;
    }
    motion.lastPointer = { x: event.clientX, y: event.clientY };
    if (event.pointerType === 'mouse') {
      motion.hoverPoint = { clientX: event.clientX, clientY: event.clientY };
      motion.hoverDirty = true;
      scheduleFrame();
    }
  });

  const endPointer = (event) => {
    if (!motion.pointers.has(event.pointerId)) return;
    motion.pointers.delete(event.pointerId);
    if (event.type === 'pointercancel') {
      motion.velocityX = 0;
      motion.velocityY = 0;
    }
    if (motion.pointers.size === 0) {
      document.body.classList.remove('is-product-dragging');
      motion.dragging = false;
      pinchStart = 0;
      motion.lastPointer = null;
      if (!motion.hoverPoint) setProductHover(false);
      if (event.pointerType === 'touch') setInspectTarget(0);
    } else if (motion.pointers.size === 1) {
      const remaining = [...motion.pointers.values()][0];
      motion.lastPointer = remaining;
      motion.dragging = true;
      pinchStart = 0;
    }
    scheduleFrame();
  };
  wrapper.addEventListener('selectstart', (event) => event.preventDefault());
  wrapper.addEventListener('dragstart', (event) => event.preventDefault());
  wrapper.addEventListener('pointerup', endPointer);
  wrapper.addEventListener('pointercancel', endPointer);
  wrapper.addEventListener('lostpointercapture', endPointer);
  wrapper.addEventListener('pointerleave', () => {
    clearInspectTimer();
    motion.hoverPoint = null;
    motion.hoverDirty = false;
    if (motion.pointers.size === 0) setProductHover(false);
    if (motion.pointers.size === 0) setInspectTarget(0);
  });
  wrapper.addEventListener('wheel', (event) => {
    if (!event.shiftKey) return;
    event.preventDefault();
    zoomBy(event.deltaY * 0.0024);
  }, { passive: false });
  wrapper.addEventListener('keydown', (event) => {
    if (event.target !== wrapper || event.isComposing || event.target.isContentEditable) return;
    const turn = 0.1;
    if (event.key === 'ArrowLeft') motion.yaw -= turn;
    else if (event.key === 'ArrowRight') motion.yaw += turn;
    else if (event.key === 'ArrowUp') motion.pitch = THREE.MathUtils.clamp(motion.pitch - turn, -1.12, 1.12);
    else if (event.key === 'ArrowDown') motion.pitch = THREE.MathUtils.clamp(motion.pitch + turn, -1.12, 1.12);
    else if (event.key === '+' || event.key === '=') zoomBy(-0.25);
    else if (event.key === '-') zoomBy(0.25);
    else return;
    event.preventDefault();
    scheduleFrame(true);
  });

  animate = (timestamp) => {
    frameHandle = 0;
    if (renderFailed || !isVisible || document.hidden) return;
    const now = timestamp || performance.now();
    const dt = Math.min(0.04, Math.max(0.001, (now - motion.lastTime) / 1000));
    motion.lastTime = now;

    if (motion.hoverDirty && motion.hoverPoint) {
      motion.hoverDirty = false;
      const isHoveringProduct = isOverProduct(motion.hoverPoint);
      setProductHover(isHoveringProduct);
      if (isHoveringProduct) scheduleHoverInspect();
      else {
        clearInspectTimer();
        if (motion.pointers.size === 0) setInspectTarget(0);
      }
    }

    if (!motion.dragging && !prefersReducedMotion) {
      motion.yaw += motion.velocityX * dt;
      motion.pitch = THREE.MathUtils.clamp(motion.pitch + motion.velocityY * dt, -1.12, 1.12);
      const damping = Math.exp(-3.4 * dt);
      motion.velocityX *= damping;
      motion.velocityY *= damping;
      if (Math.abs(motion.velocityX) < 0.0002) motion.velocityX = 0;
      if (Math.abs(motion.velocityY) < 0.0002) motion.velocityY = 0;
    }
    product.rotation.y = motion.yaw;
    product.rotation.x = motion.pitch;
    const zoomEase = prefersReducedMotion ? 1 : 1 - Math.exp(-13 * dt);
    camera.position.z += (motion.zoom - camera.position.z) * zoomEase;
    if (Math.abs(motion.zoom - camera.position.z) < 0.003) camera.position.z = motion.zoom;
    if (prefersReducedMotion) {
      motion.inspection = motion.targetInspection;
    } else {
      const progress = THREE.MathUtils.clamp((now - motion.inspectionStartedAt) / motion.inspectionDuration, 0, 1);
      const eased = progress * progress * (3 - 2 * progress);
      motion.inspection = motion.inspectionFrom + (motion.targetInspection - motion.inspectionFrom) * eased;
      if (progress === 1) motion.inspection = motion.targetInspection;
    }
    for (const material of shellMaterials) material.userData.setInspection(motion.inspection);
    inspectionWindow.material.opacity = motion.inspection * 0.12;
    inspectionWindow.visible = motion.inspection > 0.015;
    internals.visible = motion.inspection > 0.015;
    mark.material.opacity = 1 - motion.inspection;
    try {
      renderer.render(scene, camera);
    } catch (error) {
      renderFailed = true;
      console.error('RIOSE 3D rendering failed; retaining the product photograph.', error);
      wrapper.classList.remove('is-ready');
      wrapper.classList.add('is-fallback');
      fallback.alt = 'Matte yellow RIOCE livestock ear tag with black attachment pin';
      fallback.removeAttribute('aria-hidden');
      status.textContent = 'Interactive model unavailable. Showing the product photograph.';
      return;
    }
    if (!hasRendered) {
      hasRendered = true;
      wrapper.classList.add('is-ready');
      fallback.alt = '';
      fallback.setAttribute('aria-hidden', 'true');
      status.textContent = 'Interactive product model ready. Pause over the product or drag to inspect its illustrative interior.';
      warmInteriorWhenIdle();
    }
    scheduleFrame();
  };

  const stop = () => {
    if (frameHandle) {
      cancelAnimationFrame(frameHandle);
    }
    frameHandle = 0;
  };
  const start = () => {
    motion.lastTime = performance.now();
    scheduleFrame(true);
  };
  const visibilityObserver = new IntersectionObserver(([entry]) => {
    isVisible = entry.isIntersecting;
    if (isVisible) start();
    else stop();
  }, { threshold: 0.01 });
  visibilityObserver.observe(wrapper);
  document.addEventListener('visibilitychange', () => document.hidden ? stop() : start());
  scheduleFrame(true);
}

function makeTagShape(THREE, scale = 1) {
  const shape = new THREE.Shape();
  const sx = (value) => value * scale;
  const sy = (value) => value * scale;
  shape.moveTo(sx(-0.14), sy(1.2));
  shape.bezierCurveTo(sx(-0.28), sy(1.2), sx(-0.35), sy(1.05), sx(-0.35), sy(0.78));
  shape.bezierCurveTo(sx(-0.36), sy(0.55), sx(-0.57), sy(0.44), sx(-0.78), sy(0.31));
  shape.bezierCurveTo(sx(-0.96), sy(0.2), sx(-1.02), sy(0.1), sx(-1.02), sy(-0.08));
  shape.lineTo(sx(-1.02), sy(-1.12));
  shape.bezierCurveTo(sx(-1.02), sy(-1.24), sx(-0.93), sy(-1.31), sx(-0.79), sy(-1.32));
  shape.lineTo(sx(0.75), sy(-1.32));
  shape.bezierCurveTo(sx(0.9), sy(-1.32), sx(1.01), sy(-1.25), sx(1.01), sy(-1.11));
  shape.lineTo(sx(1.01), sy(-0.08));
  shape.bezierCurveTo(sx(1.01), sy(0.1), sx(0.94), sy(0.2), sx(0.77), sy(0.31));
  shape.bezierCurveTo(sx(0.57), sy(0.44), sx(0.36), sy(0.55), sx(0.35), sy(0.78));
  shape.bezierCurveTo(sx(0.35), sy(1.05), sx(0.28), sy(1.2), sx(0.14), sy(1.2));
  shape.bezierCurveTo(sx(0.06), sy(1.2), sx(-0.06), sy(1.2), sx(-0.14), sy(1.2));
  return shape;
}

function makeDomedFaceGeometry(THREE, shape, baseZ, rise) {
  // ShapeGeometry only provides boundary vertices; a radial surface keeps a
  // shallow molded face smooth when the tag is inspected at close range.
  const outline = shape.getPoints(24);
  if (outline.length > 1 && outline[0].distanceToSquared(outline[outline.length - 1]) < 1e-8) outline.pop();
  const centerX = 0;
  const centerY = -0.08;
  const ringCount = 24;
  const pointCount = outline.length;
  let signedArea = 0;
  for (let i = 0; i < pointCount; i++) {
    const current = outline[i];
    const next = outline[(i + 1) % pointCount];
    signedArea += current.x * next.y - next.x * current.y;
  }
  const isCounterClockwise = signedArea > 0;
  const positions = [centerX, centerY, domeDepth(centerX, centerY, baseZ, rise)];
  const uvs = [(centerX + 1.02) / 2.04, (centerY + 1.32) / 2.64];
  const indices = [];
  const appendVertex = (x, y) => {
    positions.push(x, y, domeDepth(x, y, baseZ, rise));
    uvs.push((x + 1.02) / 2.04, (y + 1.32) / 2.64);
  };
  for (let ring = 1; ring <= ringCount; ring++) {
    const t = ring / ringCount;
    for (const boundary of outline) {
      appendVertex(centerX + (boundary.x - centerX) * t, centerY + (boundary.y - centerY) * t);
    }
  }
  const orientedTriangle = (a, b, c) => isCounterClockwise
    ? indices.push(a, b, c)
    : indices.push(a, c, b);
  for (let point = 0; point < pointCount; point++) {
    orientedTriangle(0, 1 + point, 1 + ((point + 1) % pointCount));
  }
  for (let ring = 1; ring < ringCount; ring++) {
    const inner = 1 + (ring - 1) * pointCount;
    const outer = 1 + ring * pointCount;
    for (let point = 0; point < pointCount; point++) {
      const next = (point + 1) % pointCount;
      if (isCounterClockwise) {
        indices.push(inner + point, outer + point, outer + next, inner + point, outer + next, inner + next);
      } else {
        indices.push(inner + point, outer + next, outer + point, inner + point, inner + next, outer + next);
      }
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute('uv', new THREE.Float32BufferAttribute(uvs, 2));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();
  geometry.computeBoundingSphere();
  return geometry;
}

function makeDomedDecalGeometry(THREE, width, height, centerX, centerY, baseZ, rise) {
  const geometry = new THREE.PlaneGeometry(width, height, 12, 4);
  const positions = geometry.getAttribute('position');
  for (let i = 0; i < positions.count; i++) {
    const x = positions.getX(i) + centerX;
    const y = positions.getY(i) + centerY;
    // Keep the printed mark just above the molded face so depth-buffer
    // precision cannot make it flicker or disappear on the first frames.
    positions.setZ(i, domeDepth(x, y, baseZ, rise) + 0.008);
  }
  positions.needsUpdate = true;
  geometry.computeVertexNormals();
  return geometry;
}

function domeDepth(x, y, baseZ, rise) {
  const normalizedX = x / 1.02;
  const normalizedY = (y + 0.08) / 1.33;
  const dome = Math.max(0, 1 - normalizedX ** 2 - normalizedY ** 2);
  return baseZ + rise * dome;
}

function makeLowerWindowShape(THREE) {
  const shape = new THREE.Shape();
  shape.moveTo(-0.56, 0.12);
  shape.bezierCurveTo(-0.67, 0.12, -0.74, 0.06, -0.83, -0.02);
  shape.bezierCurveTo(-0.9, -0.09, -0.91, -0.16, -0.91, -0.28);
  shape.lineTo(-0.91, -1.02);
  shape.bezierCurveTo(-0.91, -1.15, -0.83, -1.21, -0.69, -1.21);
  shape.lineTo(0.68, -1.21);
  shape.bezierCurveTo(0.82, -1.21, 0.9, -1.15, 0.9, -1.02);
  shape.lineTo(0.9, -0.28);
  shape.bezierCurveTo(0.9, -0.16, 0.89, -0.09, 0.82, -0.02);
  shape.bezierCurveTo(0.73, 0.06, 0.66, 0.12, 0.55, 0.12);
  shape.closePath();
  return shape;
}

function makeInspectionShellMaterial(THREE, options) {
  const { inspectionFrontZ = 0.06, ...physicalOptions } = options;
  let inspectionUniform;
  let currentInspection = 0;
  const material = new THREE.MeshPhysicalMaterial({
    ...physicalOptions,
    transparent: true,
    opacity: 1,
    depthWrite: false,
  });
  material.onBeforeCompile = (shader) => {
    shader.uniforms.uInspection = { value: currentInspection };
    shader.uniforms.uInspectionFrontZ = { value: inspectionFrontZ };
    inspectionUniform = shader.uniforms.uInspection;
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\nvarying vec2 vRIOSELocalXY;\nvarying float vRIOSELocalZ;')
      .replace('#include <begin_vertex>', '#include <begin_vertex>\nvRIOSELocalXY = position.xy;\nvRIOSELocalZ = position.z;');
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', `#include <common>
        uniform float uInspection;
        uniform float uInspectionFrontZ;
        varying vec2 vRIOSELocalXY;
        varying float vRIOSELocalZ;`)
      .replace('#include <color_fragment>', `#include <color_fragment>
        float shellY = vRIOSELocalXY.y;
        float topCurve = sqrt(clamp((0.12 - shellY) / 0.14, 0.0, 1.0));
        float windowHalfWidth = mix(0.56, 0.83, topCurve);
        float shoulderCurve = sqrt(clamp((-0.02 - shellY) / 0.26, 0.0, 1.0));
        windowHalfWidth = mix(windowHalfWidth, 0.91, shoulderCurve);
        float bottomT = clamp((-1.02 - shellY) / 0.19, 0.0, 1.0);
        float bottomCurve = bottomT * bottomT;
        windowHalfWidth = mix(windowHalfWidth, 0.69, bottomCurve);
        float windowSide = 1.0 - smoothstep(windowHalfWidth - 0.012, windowHalfWidth + 0.012, abs(vRIOSELocalXY.x));
        float windowVertical = smoothstep(-1.22, -1.20, shellY) * (1.0 - smoothstep(0.115, 0.125, shellY));
        float frontSurface = smoothstep(uInspectionFrontZ - 0.006, uInspectionFrontZ + 0.006, vRIOSELocalZ);
        float inspectionWindow = windowSide * windowVertical * frontSurface;
        float smokeTint = inspectionWindow * uInspection * 0.72;
        diffuseColor.rgb = mix(diffuseColor.rgb, vec3(0.38, 0.4, 0.38), smokeTint);
        float inspectionOpacity = mix(1.0, 0.2, inspectionWindow * uInspection);
        diffuseColor.a *= inspectionOpacity;`);
  };
  material.customProgramCacheKey = () => `riose-window-shell-inspection-v4-${inspectionFrontZ}`;
  material.userData.setInspection = (value) => {
    currentInspection = value;
    if (inspectionUniform) inspectionUniform.value = value;
  };
  return material;
}

function addCylinderZ(THREE, parent, material, top, bottom, length, z, y, x = 0) {
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(top, bottom, length, 48, 1), material);
  mesh.rotation.x = Math.PI / 2;
  mesh.position.set(x, y, z);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  parent.add(mesh);
  return mesh;
}

function buildInterior(THREE, group) {
  const pcb = new THREE.Mesh(
    roundedBox(THREE, 1.3, 1.08, 0.045, 0.075),
    new THREE.MeshStandardMaterial({ color: 0x101512, roughness: 0.9, metalness: 0.025 }),
  );
  pcb.position.set(-0.015, -0.52, 0.015);
  group.add(pcb);

  const boardMaterial = new THREE.MeshStandardMaterial({ color: 0x242825, roughness: 0.56, metalness: 0.06 });
  const chip = new THREE.Mesh(roundedBox(THREE, 0.62, 0.58, 0.052, 0.025), boardMaterial);
  chip.position.set(-0.12, -0.46, 0.065);
  group.add(chip);
  const chipTop = new THREE.Mesh(roundedBox(THREE, 0.55, 0.51, 0.016, 0.018), new THREE.MeshStandardMaterial({ color: 0x1a1e1b, roughness: 0.7, metalness: 0.025 }));
  chipTop.position.set(-0.12, -0.46, 0.099);
  group.add(chipTop);

  const pinMaterial = new THREE.MeshStandardMaterial({ color: 0xb88655, roughness: 0.38, metalness: 0.7 });
  const pins = new THREE.InstancedMesh(new THREE.BoxGeometry(0.024, 0.016, 0.008), pinMaterial, 56);
  const pinTransform = new THREE.Object3D();
  let pinIndex = 0;
  for (let i = 0; i < 16; i++) {
    const y = -0.72 + i * 0.035;
    for (const x of [-0.45, 0.21]) {
      pinTransform.position.set(x, y, 0.052);
      pinTransform.updateMatrix();
      pins.setMatrixAt(pinIndex++, pinTransform.matrix);
    }
  }
  for (let i = 0; i < 12; i++) {
    const x = -0.37 + i * 0.046;
    for (const y of [-0.78, -0.14]) {
      pinTransform.position.set(x, y, 0.052);
      pinTransform.updateMatrix();
      pins.setMatrixAt(pinIndex++, pinTransform.matrix);
    }
  }
  pins.count = pinIndex;
  pins.instanceMatrix.needsUpdate = true;
  group.add(pins);

  const smallChipMaterial = new THREE.MeshStandardMaterial({ color: 0x1d231f, roughness: 0.5, metalness: 0.12 });
  for (const [x, y, w, h] of [[0.4, -0.64, 0.14, 0.12], [0.39, -0.34, 0.13, 0.1], [0.02, -0.88, 0.17, 0.11], [-0.52, -0.9, 0.1, 0.08]]) {
    const part = new THREE.Mesh(roundedBox(THREE, w, h, 0.028, 0.012), smallChipMaterial);
    part.position.set(x, y, 0.04);
    group.add(part);
  }

  const copper = new THREE.MeshStandardMaterial({ color: 0xd38b4c, roughness: 0.38, metalness: 0.58 });
  const paths = [
    [[-0.56, -0.12], [-0.56, -0.48], [-0.51, -0.68], [-0.4, -0.8], [-0.23, -0.88], [0.08, -0.88], [0.2, -0.82]],
    [[-0.51, -0.12], [-0.51, -0.48], [-0.46, -0.66], [-0.36, -0.76], [-0.2, -0.83], [0.08, -0.83], [0.2, -0.78]],
    [[-0.46, -0.12], [-0.46, -0.47], [-0.41, -0.63], [-0.32, -0.72], [-0.17, -0.78], [0.08, -0.78], [0.19, -0.73]],
    [[0.2, -0.24], [0.28, -0.24], [0.32, -0.29]],
    [[0.2, -0.66], [0.28, -0.66], [0.32, -0.64]],
  ];
  for (const coords of paths) {
    const curve = new THREE.CatmullRomCurve3(coords.map(([x, y]) => new THREE.Vector3(x, y, 0.05)));
    group.add(new THREE.Mesh(new THREE.TubeGeometry(curve, 32, 0.006, 4, false), copper));
  }

  const solder = new THREE.MeshStandardMaterial({ color: 0x9b9586, roughness: 0.4, metalness: 0.64 });
  for (const [x, y] of [[-0.59, -0.1], [0.57, -0.12], [-0.59, -0.94], [0.57, -0.94]]) {
    const screw = new THREE.Mesh(new THREE.CylinderGeometry(0.026, 0.026, 0.018, 20), solder);
    screw.position.set(x, y, 0.04);
    screw.rotation.x = Math.PI / 2;
    group.add(screw);
  }
}

function roundedBox(THREE, width, height, depth, radius) {
  const shape = new THREE.Shape();
  const x = -width / 2;
  const y = -height / 2;
  shape.moveTo(x + radius, y);
  shape.lineTo(x + width - radius, y);
  shape.quadraticCurveTo(x + width, y, x + width, y + radius);
  shape.lineTo(x + width, y + height - radius);
  shape.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
  shape.lineTo(x + radius, y + height);
  shape.quadraticCurveTo(x, y + height, x, y + height - radius);
  shape.lineTo(x, y + radius);
  shape.quadraticCurveTo(x, y, x + radius, y);
  const geometry = new THREE.ExtrudeGeometry(shape, { depth, bevelEnabled: true, bevelSegments: 3, bevelSize: 0.006, bevelThickness: 0.006 });
  geometry.translate(0, 0, -depth / 2);
  return geometry;
}

function makeMouldTexture(THREE) {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  const image = ctx.createImageData(size, size);
  let seed = 17;
  for (let i = 0; i < image.data.length; i += 4) {
    seed = (seed * 48271) % 2147483647;
    const value = 105 + (seed / 2147483647) * 44;
    image.data[i] = value;
    image.data[i + 1] = value;
    image.data[i + 2] = value;
    image.data[i + 3] = 255;
  }
  ctx.putImageData(image, 0, 0);
  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
  texture.repeat.set(3, 4);
  texture.colorSpace = THREE.NoColorSpace;
  return texture;
}

function makeMouldAlbedoTexture(THREE) {
  const size = 256;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  const image = ctx.createImageData(size, size);
  let seed = 613;
  for (let i = 0; i < image.data.length; i += 4) {
    seed = (seed * 48271) % 2147483647;
    const value = 251 + Math.floor((seed / 2147483647) * 5);
    image.data[i] = value;
    image.data[i + 1] = value;
    image.data[i + 2] = value;
    image.data[i + 3] = 255;
  }
  ctx.putImageData(image, 0, 0);
  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
  texture.repeat.set(2, 3);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  return texture;
}

function makeConnectorFaceTexture(THREE) {
  const size = 256;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  const center = size / 2;
  const base = ctx.createRadialGradient(center * 0.82, center * 0.72, 12, center, center, center);
  base.addColorStop(0, '#3a3b38');
  base.addColorStop(0.68, '#30312f');
  base.addColorStop(1, '#252624');
  ctx.fillStyle = base;
  ctx.fillRect(0, 0, size, size);

  let seed = 91;
  const random = () => {
    seed = (seed * 48271) % 2147483647;
    return seed / 2147483647;
  };
  for (let i = 0; i < 360; i++) {
    const angle = random() * Math.PI * 2;
    const inner = 22 + random() * 72;
    const outer = Math.min(125, inner + 12 + random() * 44);
    const bend = (random() - 0.5) * 0.035;
    ctx.beginPath();
    ctx.moveTo(center + Math.cos(angle) * inner, center + Math.sin(angle) * inner);
    ctx.lineTo(center + Math.cos(angle + bend) * outer, center + Math.sin(angle + bend) * outer);
    ctx.strokeStyle = random() > 0.5 ? 'rgba(172, 173, 164, .10)' : 'rgba(9, 10, 9, .17)';
    ctx.lineWidth = 0.5 + random() * 1.1;
    ctx.stroke();
  }
  for (let i = 0; i < 900; i++) {
    const angle = random() * Math.PI * 2;
    const radius = 14 + random() * 110;
    ctx.fillStyle = random() > 0.5 ? 'rgba(190, 190, 180, .11)' : 'rgba(5, 6, 5, .15)';
    ctx.fillRect(center + Math.cos(angle) * radius, center + Math.sin(angle) * radius, 0.7, 0.7);
  }

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  return texture;
}

function makeStudioEnvironment(THREE) {
  const canvas = document.createElement('canvas');
  canvas.width = 512;
  canvas.height = 256;
  const ctx = canvas.getContext('2d');
  const base = ctx.createLinearGradient(0, 0, 0, canvas.height);
  base.addColorStop(0, '#a7a69f');
  base.addColorStop(0.48, '#777873');
  base.addColorStop(1, '#aaa89f');
  ctx.fillStyle = base;
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  const softboxes = [
    [34, 66, 110, 18], [174, 38, 148, 25], [356, 71, 118, 17],
    [94, 178, 82, 13], [278, 191, 154, 17], [453, 139, 35, 66],
  ];
  for (const [x, y, width, height] of softboxes) {
    ctx.save();
    ctx.shadowColor = 'rgba(255, 253, 240, .76)';
    ctx.shadowBlur = 24;
    ctx.fillStyle = 'rgba(255, 253, 240, .72)';
    ctx.fillRect(x, y, width, height);
    ctx.restore();
  }

  const texture = new THREE.CanvasTexture(canvas);
  texture.mapping = THREE.EquirectangularReflectionMapping;
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.needsUpdate = true;
  return texture;
}

function makeWordmarkTexture(THREE) {
  const canvas = document.createElement('canvas');
  canvas.width = 480;
  canvas.height = 112;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#34342f';
  ctx.font = '600 58px Helvetica, Arial, sans-serif';
  ctx.textBaseline = 'middle';
  const letters = 'RIOSE';
  const spacing = 8;
  const total = [...letters].reduce((sum, letter) => sum + ctx.measureText(letter).width, 0) + spacing * (letters.length - 1);
  let x = (canvas.width - total) / 2;
  for (const letter of letters) {
    ctx.fillText(letter, x, canvas.height / 2 + 2);
    x += ctx.measureText(letter).width + spacing;
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function makeShadowTexture(THREE) {
  const canvas = document.createElement('canvas');
  canvas.width = 256;
  canvas.height = 128;
  const ctx = canvas.getContext('2d');
  const gradient = ctx.createRadialGradient(128, 64, 4, 128, 64, 62);
  gradient.addColorStop(0, 'rgba(40, 36, 27, .42)');
  gradient.addColorStop(.55, 'rgba(40, 36, 27, .16)');
  gradient.addColorStop(1, 'rgba(40, 36, 27, 0)');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  return new THREE.CanvasTexture(canvas);
}
