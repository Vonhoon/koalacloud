/**
 * game.js
 * This file handles all game logic and socket communication.
 * It controls sketch.js.
 */

// --- Game State ---
let socket;
let opponent_sid = null;
let myTurn = false;
let gameOver = false;
let rollsLeft = 3;
let dice = [1, 1, 1, 1, 1];
let kept = [false, false, false, false, false];

// Scorecards
const CATEGORIES = [
  'aces', 'twos', 'threes', 'fours', 'fives', 'sixes',
  'four_of_a_kind', 'full_house', 'small_straight', 'large_straight', 'yacht', 'chance'
];
let myScorecard = {};
let opponentScorecard = {};
CATEGORIES.forEach(cat => {
  myScorecard[cat] = null;
  opponentScorecard[cat] = null;
});

// --- DOM Elements ---
let waitingScreen, gameScreen, scoreSheet, rollButton, shakeButton, scoreTabButton, scoreSheetClose, turnIndicator, connectionStatus;

// --- Entry Point ---
window.onload = function() {
  // Get all DOM elements
  waitingScreen = document.getElementById('waiting-screen');
  gameScreen = document.getElementById('game-screen');
  scoreSheet = document.getElementById('score-sheet');
  rollButton = document.getElementById('roll-button');
  shakeButton = document.getElementById('shake-button');
  scoreTabButton = document.getElementById('score-tab-button');
  scoreSheetClose = document.getElementById('score-sheet-close');
  turnIndicator = document.getElementById('turn-indicator');
  connectionStatus = document.getElementById('connection-status');

  // --- Setup Button Clicks ---
  rollButton.onclick = onRollClicked;
  shakeButton.onclick = onShakeClicked;
  scoreTabButton.onclick = onScoreTabClicked;
  scoreSheetClose.onclick = onScoreTabClicked; // Also closes

  // Add click listeners to all *my* score categories
  document.querySelectorAll('#my-score-column .score-category').forEach(el => {
    el.onclick = () => onScoreCategoryClicked(el.dataset.category);
  });

  // --- Connect to Server ---
  connectionStatus.textContent = 'Connecting to server...';
  socket = io();

  // --- Socket.IO Listeners ---
  socket.on('connect', () => {
    connectionStatus.textContent = 'Connected. Finding game...';
    socket.emit('find_game');
  });

  socket.on('waiting_for_opponent', () => {
    connectionStatus.textContent = 'Waiting for an opponent...';
  });

  socket.on('game_start', (data) => {
    opponent_sid = data.opponent_sid;
    myTurn = data.my_turn;
    
    waitingScreen.classList.add('hidden');
    gameScreen.classList.remove('hidden');
    setTimeout(() => {
        if (typeof sketch_initCanvas === 'function') {
            sketch_initCanvas();
        }
    }, 0);
    startNewTurn(); // Resets dice and UI
  });
  
  socket.on('action_from_opponent', (action) => {
    if (gameOver) return;
    
    switch (action.type) {
      case 'roll':
        dice = action.dice;
        sketch_setDice(dice); // Tell p5.js to draw
        break;
      case 'keep':
        kept = action.kept;
        sketch_setKept(kept); // Tell p5.js to draw
        break;
      case 'score':
        // Opponent has scored and ended their turn
        opponentScorecard[action.category] = action.score;
        myTurn = true;
        startNewTurn();
        checkGameOver();
        break;
    }
    updateUI();
  });
  
  socket.on('player_disconnected', (data) => {
    if (data.sid === opponent_sid) {
      alert('Opponent disconnected. Game over.');
      gameOver = true;
      myTurn = false;
      updateUI();
    }
  });

  socket.on('disconnect', () => {
    connectionStatus.textContent = 'Disconnected from server.';
    if (!gameOver) {
      alert('Connection lost. Please refresh.');
      gameOver = true;
      myTurn = false;
      updateUI();
    }
  });
}

// --- Button Click Handlers ---
function onRollClicked() {
  if (!myTurn || rollsLeft === 0 || gameOver) return;

  // 1. Start shake animation
  sketch_startShakeAnimation();
  
  // 2. Wait for animation
  setTimeout(() => {
    rollsLeft--;
    
    // 3. Calculate new dice values
    for (let i = 0; i < 5; i++) {
      if (!kept[i]) {
        dice[i] = Math.floor(Math.random() * 6) + 1;
      }
    }
    
    // 4. Update my UI
    sketch_setDice(dice);
    updateUI();
    sendAction({ type: 'roll', dice: dice });
    
    // 5. If 0 rolls left, force scores open
    if (rollsLeft === 0) {
      showEstimatedScores();
      scoreSheet.classList.add('slide-in');
    }
  }, 500); // Wait for shake animation
}

function onShakeClicked() {
  if (myTurn && rollsLeft > 0 && !gameOver) {
    sketch_startShakeAnimation();
  }
}

function onScoreTabClicked() {
  scoreSheet.classList.toggle('slide-in');
  if (myTurn && rollsLeft < 3) {
    showEstimatedScores();
  } else {
    clearEstimatedScores();
  }
}

// --- Functions called BY sketch.js ---
function game_onDieClicked(index) {
  if (!myTurn || rollsLeft === 3 || rollsLeft === 0 || gameOver) return;
  
  kept[index] = !kept[index];
  sketch_setKept(kept); // Update p5.js
  sendAction({ type: 'keep', kept: kept });
}

function game_onDeviceShake() {
  onShakeClicked();
}

// --- Game Logic Functions ---
function showEstimatedScores() {
  clearEstimatedScores();
  for (const category of CATEGORIES) {
    if (myScorecard[category] === null) {
      const score = calculateScore(category, dice);
      const el = document.getElementById(`my-${category}`);
      el.classList.add('estimated');
      el.querySelector('span').textContent = score;
    }
  }
}

function clearEstimatedScores() {
  document.querySelectorAll('#my-score-column .score-category.estimated').forEach(el => {
    el.classList.remove('estimated');
    // If not locked, reset text to '-'
    const category = el.dataset.category;
    if (myScorecard[category] === null) {
      el.querySelector('span').textContent = '-';
    }
  });
}

function onScoreCategoryClicked(category) {
  if (!myTurn || rollsLeft === 3 || gameOver) return;
  if (myScorecard[category] !== null) return; // Already taken
  
  // 1. Lock in the score
  const score = calculateScore(category, dice);
  myScorecard[category] = score;

  // 2. Tell opponent
  sendAction({ type: 'score', category: category, score: score });
  
  // 3. End my turn
  myTurn = false;
  scoreSheet.classList.remove('slide-in');
  startNewTurn();
  checkGameOver();
  updateUI();
}

function startNewTurn() {
  rollsLeft = 3;
  dice = [1, 1, 1, 1, 1];
  kept = [false, false, false, false, false];
  
  // Don't set dice for opponent's turn, just reset UI
  if (myTurn) {
    sketch_setDice(dice);
  }
  sketch_setKept(kept);
  clearEstimatedScores();
  updateUI();
}

function updateUI() {
  // Update roll button
  rollButton.textContent = `Roll (${rollsLeft})`;
  rollButton.disabled = !myTurn || rollsLeft === 0 || gameOver;
  shakeButton.disabled = !myTurn || rollsLeft === 0 || gameOver;

  // Update turn indicator
  if (gameOver) {
    const myTotal = calculateTotalScore(myScorecard);
    const oppTotal = calculateTotalScore(opponentScorecard);
    if (myTotal > oppTotal) {
      turnIndicator.textContent = "You Win!";
    } else if (oppTotal > myTotal) {
      turnIndicator.textContent = "You Lose.";
    } else {
      turnIndicator.textContent = "It's a Tie!";
    }
  } else if (myTurn) {
    turnIndicator.textContent = "Your Turn";
  } else {
    turnIndicator.textContent = "Opponent's Turn";
  }
  
  // Update scorecards
  updateScorecardUI('my', myScorecard);
  updateScorecardUI('opp', opponentScorecard);
}

function updateScorecardUI(prefix, scorecard) {
  let upperTotal = 0;
  let lowerTotal = 0;

  for (const category of CATEGORIES) {
    const score = scorecard[category];
    const el = document.getElementById(`${prefix}-${category}`);
    if (score !== null) {
      el.classList.add('locked');
      el.querySelector('span').textContent = score;
      
      // Add to totals
      if (['aces', 'twos', 'threes', 'fours', 'fives', 'sixes'].includes(category)) {
        upperTotal += score;
      } else {
        lowerTotal += score;
      }
    }
  }
  
  document.getElementById(`${prefix}-upper-total`).textContent = upperTotal;
  
  // Check for upper bonus
  let bonus = 0;
  if (upperTotal >= 63) {
    bonus = 35;
    document.getElementById(`${prefix}-upper-bonus`).querySelector('span').textContent = `+${bonus}`;
  } else {
    document.getElementById(`${prefix}-upper-bonus`).querySelector('span').textContent = `+0`;
  }
  
  document.getElementById(`${prefix}-lower-total`).textContent = lowerTotal;
  document.getElementById(`${prefix}-total-score`).textContent = upperTotal + lowerTotal + bonus;
}

function checkGameOver() {
  // Check if all categories in *my* scorecard are filled
  for (const category of CATEGORIES) {
    if (myScorecard[category] === null) {
      return; // Not over yet
    }
  }
  // If we get here, my board is full. The opponent just finished their last turn.
  gameOver = true;
  myTurn = false;
  updateUI();
}

function sendAction(actionObject) {
  if (gameOver) return;
  socket.emit('game_action', {
    opponent_sid: opponent_sid,
    action: actionObject
  });
}

// --- YACHT SCORING LOGIC ---

function calculateScore(category, dice) {
  const counts = {}; // Count occurrences of each die
  let sum = 0;
  for (const die of dice) {
    counts[die] = (counts[die] || 0) + 1;
    sum += die;
  }
  const sortedDice = [...dice].sort();
  const uniqueDice = Object.keys(counts).map(Number);
  const countsArr = Object.values(counts);

  switch (category) {
    case 'aces': return (counts[1] || 0) * 1;
    case 'twos': return (counts[2] || 0) * 2;
    case 'threes': return (counts[3] || 0) * 3;
    case 'fours': return (counts[4] || 0) * 4;
    case 'fives': return (counts[5] || 0) * 5;
    case 'sixes': return (counts[6] || 0) * 6;
    case 'chance': return sum;
    
    case 'four_of_a_kind':
      if (countsArr.some(c => c >= 4)) {
        return sum;
      }
      return 0;

    case 'full_house':
      if (countsArr.includes(3) && countsArr.includes(2)) {
        return 25;
      }
      return 0;
      
    case 'small_straight':
      // Check for 1,2,3,4 or 2,3,4,5 or 3,4,5,6
      if (uniqueDice.join('').includes('1234')) return 30;
      if (uniqueDice.join('').includes('2345')) return 30;
      if (uniqueDice.join('').includes('3456')) return 30;
      return 0;

    case 'large_straight':
      if (uniqueDice.join('') === '12345' || uniqueDice.join('') === '23456') {
        return 40;
      }
      return 0;

    case 'yacht':
      if (countsArr.some(c => c === 5)) {
        return 50;
      }
      return 0;
      
    default:
      return 0;
  }
}

function calculateTotalScore(scorecard) {
  let upperTotal = 0;
  let lowerTotal = 0;
  for (const category of CATEGORIES) {
    const score = scorecard[category] || 0;
    if (['aces', 'twos', 'threes', 'fours', 'fives', 'sixes'].includes(category)) {
      upperTotal += score;
    } else {
      lowerTotal += score;
    }
  }
  let bonus = (upperTotal >= 63) ? 35 : 0;
  return upperTotal + lowerTotal + bonus;
}