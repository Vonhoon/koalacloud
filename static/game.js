/**
 * game.js
 * This file handles all game logic and socket communication.
 * It controls sketch.js.
 */

// --- Game State ---
let socket;
let isSinglePlayer = false; 
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
function resetScorecards() { 
  CATEGORIES.forEach(cat => {
    myScorecard[cat] = null;
    opponentScorecard[cat] = null;
  });
}

// --- DOM Elements ---
let modeSelectionScreen, waitingScreen, gameScreen, rollButton, turnIndicator, connectionStatus;
// NEW: Scoreboard headers
let myScoreHeaderLabel, oppScoreHeaderLabel;


// --- Entry Point ---
document.addEventListener('DOMContentLoaded', function() {
  // Get all DOM elements
  modeSelectionScreen = document.getElementById('mode-selection-screen');
  waitingScreen = document.getElementById('waiting-screen');
  gameScreen = document.getElementById('game-screen');
  rollButton = document.getElementById('roll-button');
  turnIndicator = document.getElementById('turn-indicator');
  connectionStatus = document.getElementById('connection-status');
  
  // NEW: Scoreboard header labels
  myScoreHeaderLabel = document.getElementById('my-score-header-label');
  oppScoreHeaderLabel = document.getElementById('opp-score-header-label');

  // --- Setup Button Clicks ---
  
  document.getElementById('btn-single-player').onclick = startGameSinglePlayer;
  document.getElementById('btn-multiplayer').onclick = startGameMultiplayer;

  rollButton.onclick = onRollClicked;
  
  // Add click listeners to all *my* score categories
  document.querySelectorAll('.score-category').forEach(el => {
    el.onclick = () => onScoreCategoryClicked(el.dataset.category);
  });
});

// --- Game Mode Start Functions ---

function startGameSinglePlayer() {
  isSinglePlayer = true;
  modeSelectionScreen.classList.add('hidden');
  gameScreen.classList.remove('hidden');
  
  // Set opponent name to "AI"
  myScoreHeaderLabel.textContent = "나의 점수";
  oppScoreHeaderLabel.textContent = "AI 점수";

  setTimeout(() => {
    if (typeof sketch_initCanvas === 'function') {
        sketch_initCanvas();
    }
  }, 0);
  
  resetScorecards();
  myTurn = true; // Player always starts
  startNewTurn();
}

function startGameMultiplayer() {
  isSinglePlayer = false;
  modeSelectionScreen.classList.add('hidden');
  waitingScreen.classList.remove('hidden');
  
  // Set opponent name
  myScoreHeaderLabel.textContent = "나의 점수";
  oppScoreHeaderLabel.textContent = "상대 점수";

  connectionStatus.textContent = '서버에 연결 중...';
  socket = io();

  // --- Socket.IO Listeners ---
  socket.on('connect', () => {
    connectionStatus.textContent = '연결됨. 게임 찾는 중...';
    socket.emit('find_game');
  });

  socket.on('waiting_for_opponent', () => {
    connectionStatus.textContent = '상대방을 기다리는 중...';
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
    
    resetScorecards();
    startNewTurn(); 
  });
  
  socket.on('action_from_opponent', (action) => {
    if (gameOver || isSinglePlayer) return;
    
    switch (action.type) {
      case 'roll':
        dice = action.dice;
        sketch_setDice(dice); 
        break;
      case 'keep':
        kept = action.kept;
        sketch_setKept(kept); 
        break;
      case 'score':
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
      alert('상대방의 연결이 끊겼습니다. 게임 종료.');
      gameOver = true;
      myTurn = false;
      updateUI();
    }
  });

  socket.on('disconnect', () => {
    connectionStatus.textContent = '서버와 연결이 끊겼습니다.';
    if (!gameOver) {
      alert('연결이 끊겼습니다. 새로고침 해주세요.');
      gameOver = true;
      myTurn = false;
      updateUI();
    }
  });
}

// --- Button Click Handlers ---

function onRollClicked() {
  if (!myTurn || rollsLeft === 0 || gameOver) return;

  rollsLeft--;
  
  for (let i = 0; i < 5; i++) {
    if (!kept[i]) {
      dice[i] = Math.floor(Math.random() * 6) + 1;
    }
  }
  
  sketch_setDice(dice);
  
  // --- YACHT CHECK ---
  if (myScorecard['yacht'] === null) {
    const yachtScore = calculateScore('yacht', dice);
    if (yachtScore === 50) {
      triggerConfetti(); 
      rollsLeft = 0; 
      kept = [true, true, true, true, true]; 
      sketch_setKept(kept);
      updateUI(); 
      
      setTimeout(() => {
        onScoreCategoryClicked('yacht');
      }, 2000); 
      
      return; 
    }
  }
  // --- END YACHT CHECK ---

  updateUI();
  sendAction({ type: 'roll', dice: dice });
  
  showEstimatedScores();
}

// --- Functions called BY sketch.js ---
function game_onDieClicked(index) {
  // Check p5Dice[i] exists before trying to access it
  if (!p5Dice[index]) return; 

  if (!myTurn || rollsLeft === 3 || rollsLeft === 0 || gameOver) return;
  
  kept[index] = !kept[index];
  sketch_setKept(kept); 
  sendAction({ type: 'keep', kept: kept });
}

// --- Game Logic Functions ---
function showEstimatedScores() {
  clearEstimatedScores();
  for (const category of CATEGORIES) {
    if (myScorecard[category] === null) {
      const score = calculateScore(category, dice);
      // Update the correct span
      const el = document.getElementById(`my-${category}`);
      if (el) {
        el.textContent = score;
        el.parentElement.classList.add('estimated');
      }
    }
  }
}

function clearEstimatedScores() {
  document.querySelectorAll('.score-category.estimated').forEach(el => {
    el.classList.remove('estimated');
    const category = el.dataset.category;
    if (myScorecard[category] === null) {
      // Find the "my-score" span inside this element
      const scoreSpan = el.querySelector(`#my-${category}`);
      if (scoreSpan) {
        scoreSpan.textContent = '-';
      }
    }
  });
}

function onScoreCategoryClicked(category) {
  if (!myTurn || rollsLeft === 3 || gameOver) return; 
  if (myScorecard[category] !== null) return; 
  
  const score = calculateScore(category, dice);
  myScorecard[category] = score;

  sendAction({ type: 'score', category: category, score: score });
  
  myTurn = false;
  startNewTurn();
  checkGameOver();
  updateUI();
  
  if (isSinglePlayer && !gameOver) {
    setTimeout(ai_takeTurn, 1000); 
  }
}

function startNewTurn() {
  rollsLeft = 3;
  dice = [1, 1, 1, 1, 1];
  kept = [false, false, false, false, false];
  
  if (myTurn) {
    sketch_setDice(dice);
  }
  sketch_setKept(kept);
  clearEstimatedScores();
  updateUI();
}

function updateUI() {
  rollButton.textContent = `굴리기 (${rollsLeft})`;
  rollButton.disabled = !myTurn || rollsLeft === 0 || gameOver;

  // Update Scoreboard
  updateScoreboard();

  // Update Turn Indicator
  if (gameOver) {
    const myTotal = calculateTotalScore(myScorecard);
    const oppTotal = calculateTotalScore(opponentScorecard);
    if (myTotal > oppTotal) {
      turnIndicator.textContent = "승리!";
    } else if (oppTotal > myTotal) {
      turnIndicator.textContent = "패배.";
    } else {
      turnIndicator.textContent = "무승부!";
    }
  } else if (myTurn) {
    turnIndicator.textContent = "내 차례";
  } else {
    turnIndicator.textContent = isSinglePlayer ? "AI 차례" : "상대방 차례";
  }
}

// NEW: Unified scoreboard update function
function updateScoreboard() {
  let myUpperTotal = 0;
  let myLowerTotal = 0;
  let oppUpperTotal = 0;
  let oppLowerTotal = 0;

  // Update all category rows
  for (const category of CATEGORIES) {
    const myScore = myScorecard[category];
    const oppScore = opponentScorecard[category];
    
    const myEl = document.getElementById(`my-${category}`);
    const oppEl = document.getElementById(`opp-${category}`);
    const rowEl = myEl ? myEl.parentElement : null;

    if (myEl && oppEl && rowEl) {
      // Update my score
      if (myScore !== null) {
        myEl.textContent = myScore;
        rowEl.classList.add('locked');
        if (['aces', 'twos', 'threes', 'fours', 'fives', 'sixes'].includes(category)) {
          myUpperTotal += myScore;
        } else {
          myLowerTotal += myScore;
        }
      } else if (!rowEl.classList.contains('estimated')) {
        myEl.textContent = '-';
      }

      // Update opponent's score
      if (oppScore !== null) {
        oppEl.textContent = oppScore;
        if (['aces', 'twos', 'threes', 'fours', 'fives', 'sixes'].includes(category)) {
          oppUpperTotal += oppScore;
        } else {
          oppLowerTotal += oppScore;
        }
      } else {
        oppEl.textContent = '-';
      }

      // Manage 'locked' state (only applies to *my* side)
      if (myScore === null) {
        rowEl.classList.remove('locked');
      }
    }
  }
  
  // Update Upper Totals
  document.getElementById('my-upper-total').textContent = myUpperTotal;
  document.getElementById('opp-upper-total').textContent = oppUpperTotal;

  // Update Bonuses
  let myBonus = (myUpperTotal >= 63) ? 35 : 0;
  let oppBonus = (oppUpperTotal >= 63) ? 35 : 0;
  document.getElementById('my-upper-bonus').textContent = `+${myBonus}`;
  document.getElementById('opp-upper-bonus').textContent = `+${oppBonus}`;
  
  // Update Lower Totals
  document.getElementById('my-lower-total').textContent = myLowerTotal;
  document.getElementById('opp-lower-total').textContent = oppLowerTotal;
  
  // Update Grand Totals
  document.getElementById('my-total-score').textContent = myUpperTotal + myLowerTotal + myBonus;
  document.getElementById('opp-total-score').textContent = oppUpperTotal + oppLowerTotal + oppBonus;
}


function checkGameOver() {
  for (const category of CATEGORIES) {
    if (myScorecard[category] === null) {
      return; 
    }
  }
  
  // NEW: Also check opponent's card in single player
  if (isSinglePlayer) {
    for (const category of CATEGORIES) {
      if (opponentScorecard[category] === null) {
        return; 
      }
    }
  }

  gameOver = true;
  myTurn = false;
  updateUI();
}

function sendAction(actionObject) {
  if (gameOver || isSinglePlayer) return; 
  socket.emit('game_action', {
    opponent_sid: opponent_sid,
    action: actionObject
  });
}

// --- YACHT SCORING LOGIC ---
function calculateScore(category, dice) {
  const counts = {}; 
  let sum = 0;
  for (const die of dice) {
    counts[die] = (counts[die] || 0) + 1;
    sum += die;
  }
  const sortedDice = [...dice].sort();
  const uniqueDice = Object.keys(counts).map(Number).sort().join('');
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
      if (uniqueDice.includes('1234') || uniqueDice.includes('2345') || uniqueDice.includes('3456')) {
        return 30;
      }
      return 0;

    case 'large_straight':
      if (uniqueDice === '12345' || uniqueDice === '23456') {
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


// --- AI LOGIC ---
let aiRollsLeft = 3;
let aiKept = [false, false, false, false, false];
let aiDice = [1, 1, 1, 1, 1];

function ai_takeTurn() {
  if (myTurn || gameOver) return;
  
  aiRollsLeft = 3;
  aiKept = [false, false, false, false, false];
  aiDice = [1, 1, 1, 1, 1];
  sketch_setKept(aiKept);
  
  ai_roll();
}

function ai_roll() {
  aiRollsLeft--;

  for (let i = 0; i < 5; i++) {
    if (!aiKept[i]) {
      aiDice[i] = Math.floor(Math.random() * 6) + 1;
    }
  }
  sketch_setDice(aiDice);
  
  if (aiRollsLeft > 0) {
    const decision = ai_decideKeep(aiDice);
    aiKept = decision.kept;
    sketch_setKept(aiKept);
    
    setTimeout(ai_roll, 1500); 
  } else {
    setTimeout(ai_score, 1500);
  }
}

function ai_decideKeep(dice) {
  const available = CATEGORIES.filter(cat => opponentScorecard[cat] === null);
  
  // 1. Check for immediate high scores
  for (const cat of available) {
    const score = calculateScore(cat, dice);
    if (cat === 'yacht' && score === 50) {
      return { category: 'yacht', score: 50, kept: [true,true,true,true,true] };
    }
    if (cat === 'large_straight' && score === 40) {
      return { category: 'large_straight', score: 40, kept: [true,true,true,true,true] };
    }
    if (cat === 'full_house' && score === 25) {
      return { category: 'full_house', score: 25, kept: [true,true,true,true,true] };
    }
  }
  
  // 2. Decide what to hold
  const counts = {};
  for (const die of dice) { counts[die] = (counts[die] || 0) + 1; }
  
  let maxCount = 0;
  let dieToKeep = 0;
  for (let i = 6; i >= 1; i--) { 
    if ((counts[i] || 0) >= maxCount) {
      maxCount = counts[i];
      dieToKeep = i;
    }
  }

  let newKept = [false, false, false, false, false];
  if (maxCount > 1) { 
    for (let i = 0; i < 5; i++) {
      if (dice[i] === dieToKeep) {
        newKept[i] = true;
      }
    }
  }
  
  return { kept: newKept };
}

function ai_score() {
  const available = CATEGORIES.filter(cat => opponentScorecard[cat] === null);
  let bestCategory = 'chance';
  let maxScore = -1; // Use -1 to allow 0 scores

  for (const cat of available) {
    const score = calculateScore(cat, aiDice);
    if (score > maxScore) {
      maxScore = score;
      bestCategory = cat;
    }
  }
  
  if (maxScore <= 0) {
    maxScore = 0; // Ensure score is 0, not -1
    // Sacrifice an upper category if available
    const upperSacrifice = ['aces', 'twos', 'threes'].find(cat => available.includes(cat));
    if (upperSacrifice) {
      bestCategory = upperSacrifice;
    } else if (available.includes('chance')) {
      bestCategory = 'chance';
    } else {
      bestCategory = available[0]; 
    }
  }
  
  opponentScorecard[bestCategory] = maxScore;
  
  myTurn = true;
  startNewTurn();
  checkGameOver();
}

// --- Confetti Function ---
function triggerConfetti() {
  const container = document.getElementById('confetti-container');
  if (!container) return;

  const particleCount = 100; 
  const shapes = ['square', 'circle', 'triangle'];
  const colors = [
    '#0ea5e9', 
    '#facc15', 
    '#f87171', 
    '#4ade80', 
    '#a78bfa'  
  ];

  for (let i = 0; i < particleCount; i++) {
    const particle = document.createElement('div');
    
    const shape = shapes[Math.floor(Math.random() * shapes.length)];
    particle.className = `confetti-particle ${shape}`;
    
    const color = colors[Math.floor(Math.random() * colors.length)];
    if (shape === 'triangle') {
      particle.style.borderBottomColor = color;
    } else {
      particle.style.backgroundColor = color;
    }
    
    particle.style.left = Math.random() * 100 + 'vw';
    
    particle.style.animationDelay = Math.random() * 0.5 + 's';
    particle.style.animationDuration = (Math.random() * 1.5 + 1) + 's';

    container.appendChild(particle);
    
    setTimeout(() => {
      particle.remove();
    }, 2500);
  }
}