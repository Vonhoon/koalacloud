/**
 * sketch.js
 * This file handles all p5.js drawing.
 * It is controlled by game.js.
 */

let p5Dice = []; // Array of 5 Die objects
let isShaking = false;
let shakeTime = 0;
const SHAKE_DURATION = 500; // ms

let canvas; // To hold the p5 canvas

// --- p5.js setup() ---
// --- p5.js setup() ---
function setup() {
  // P5.js initialization, but canvas creation is deferred until the game starts.
  window.addEventListener('devicemotion', onDeviceShake);
}

// --- NEW FUNCTION called BY game.js to create the canvas ---
function sketch_initCanvas() {
  let container = document.getElementById('canvas-container');
  let w = container.clientWidth;
  let h = Math.min(container.clientHeight, 400); 
  
  // Fallback if dimensions are somehow still zero
  if (w <= 0 || h <= 0) { w = 400; h = 200; }

  // Create the canvas and attach it to the container
  canvas = createCanvas(w, h);
  canvas.parent('canvas-container');

  // Reposition dice (initial creation)
  let spacing = w / 6;
  let dieSize = Math.min(spacing * 0.8, 60);
  let yPos = h / 2 - dieSize / 2;
  for (let i = 0; i < 5; i++) {
    let xPos = spacing * (i + 1) - dieSize / 2;
    p5Dice[i] = new Die(xPos, yPos, dieSize);
  }
}



// --- p5.js draw() ---
function draw() {
  background(248, 250, 252); // var(--bg-light)

  let now = millis();

  // Stop shaking after duration
  if (isShaking && now - shakeTime > SHAKE_DURATION) {
    isShaking = false;
  }

  // Loop and draw all 5 dice
  for (let die of p5Dice) {
    if (isShaking && !die.isKept) {
      die.animateShake();
    }
    die.draw();
  }
}

// --- p5.js built-in resize ---
function windowResized() {
  if (!canvas) return; // Only run after canvas is created
  let container = document.getElementById('canvas-container');
  let w = container.clientWidth;
  let h = Math.min(container.clientHeight, 400);

  // Reposition dice
  let spacing = w / 6;
  let dieSize = Math.min(spacing * 0.8, 60);
  let yPos = h / 2 - dieSize / 2;
  for (let i = 0; i < 5; i++) {
    p5Dice[i].x = spacing * (i + 1) - dieSize / 2;
    p5Dice[i].y = yPos;
    p5Dice[i].size = dieSize;
  }
}

// --- Die Class ---
class Die {
  constructor(x, y, size) {
    this.x = x;
    this.y = y;
    this.size = size;
    this.value = 1;
    this.isKept = false;
  }

  draw() {
    push(); // Save drawing state
    translate(this.x, this.y);

    // Draw the die body
    strokeWeight(2);
    stroke(this.isKept ? '#facc15' : '#0f172a'); // var(--kept-border) : var(--ink)
    fill(this.isKept ? '#fef08a' : 'white'); // var(--kept)
    rect(0, 0, this.size, this.size, this.size * 0.1); // Rounded corners

    // Draw the pips
    fill('#0f172a'); // var(--ink)
    noStroke();
    this._drawPips(this.value);
    
    pop(); // Restore drawing state
  }

  _drawPips(val) {
    let s = this.size;
    let c = s / 2; // center
    let q = s / 4; // quarter
    let e = s - q; // other quarter
    let d = s / 5; // pip diameter
    
    // Center dot
    if ([1, 3, 5].includes(val)) {
      ellipse(c, c, d, d);
    }
    // Top-left and bottom-right
    if ([2, 3, 4, 5, 6].includes(val)) {
      ellipse(q, q, d, d);
      ellipse(e, e, d, d);
    }
    // Top-right and bottom-left
    if ([4, 5, 6].includes(val)) {
      ellipse(e, q, d, d);
      ellipse(q, e, d, d);
    }
    // Middle-left and middle-right
    if (val === 6) {
      ellipse(q, c, d, d);
      ellipse(e, c, d, d);
    }
  }

  animateShake() {
    // Jiggle the die
    translate(random(-2, 2), random(-2, 2));
  }

  isClicked(mx, my) {
    return (mx > this.x && mx < this.x + this.size &&
            my > this.y && my < this.y + this.size);
  }
}

// --- Event Handlers ---
function mousePressed() {
  // Check if p5.js exists and if game_onDieClicked is defined
  if (typeof game_onDieClicked === 'function') {
    for (let i = 0; i < 5; i++) {
      if (p5Dice[i].isClicked(mouseX, mouseY)) {
        // Tell game.js that a die was clicked
        game_onDieClicked(i);
      }
    }
  }
  return false; // Prevent default
}

function onDeviceShake(event) {
  let acc = event.accelerationIncludingGravity;
  // Simple shake detection
  if (abs(acc.x) > 15 || abs(acc.y) > 15 || abs(acc.z) > 15) {
    if (typeof game_onDeviceShake === 'function') {
      game_onDeviceShake();
    }
  }
}


// --- Functions called BY game.js ---
function sketch_setDice(diceValues) {
  if (p5Dice.length === 5) {
    for (let i = 0; i < 5; i++) {
      p5Dice[i].value = diceValues[i];
    }
  }
}

function sketch_setKept(keptArray) {
  if (p5Dice.length === 5) {
    for (let i = 0; i < 5; i++) {
      p5Dice[i].isKept = keptArray[i];
    }
  }
}

function sketch_startShakeAnimation() {
  isShaking = true;
  shakeTime = millis(); // Get current p5 time
}