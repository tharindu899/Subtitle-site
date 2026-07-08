(() => {
  const requestJson = async (url, options = {}) => {
    const response = await fetch(url, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    })
    let data = null
    try { data = await response.json() } catch { /* Downloads intentionally do not return JSON. */ }
    if (!response.ok) throw new Error(data?.detail || 'Request failed. Please try again.')
    return data
  }

  const searchSheet = document.querySelector('[data-search-sheet]')
  const searchInput = searchSheet?.querySelector('[data-search-input]')
  const mobileMenu = document.querySelector('[data-mobile-menu]')
  const navDock = document.querySelector('[data-site-nav-dock]')
  const navSpacer = document.querySelector('[data-site-nav-spacer]')

  // The ticker keeps the original two-lane, -50% marquee motion. The saved ad
  // code is parsed once; the second lane is a script-free visual clone, so ad
  // scripts cannot run twice or stack duplicate text/buttons on top of each other.
  const buildTickerLoopCopy = (ticker) => {
    const source = ticker.querySelector('[data-ad-source]')
    const target = ticker.querySelector('[data-ad-loop-copy]')
    if (!source || !target || target.childNodes.length) return

    const copy = source.cloneNode(true)
    copy.removeAttribute('data-ad-source')
    copy.removeAttribute('id')
    copy.querySelectorAll('script').forEach((script) => script.remove())
    copy.querySelectorAll('[id]').forEach((node) => node.removeAttribute('id'))
    copy.querySelectorAll('input, button, select, textarea').forEach((node) => {
      node.tabIndex = -1
      node.setAttribute('aria-hidden', 'true')
    })
    target.replaceChildren(...copy.childNodes)
  }

  document.querySelectorAll('[data-ad-ticker]').forEach(buildTickerLoopCopy)

  // The dock is fixed (not sticky), so it cannot disappear while a page scrolls.
  // Keep an equal spacer in the document flow; ResizeObserver also covers ad images
  // or iframes that finish loading after the first paint.
  const syncNavDockHeight = () => {
    if (!navDock || !navSpacer) return
    const height = Math.ceil(navDock.getBoundingClientRect().height)
    if (!height) return
    const value = `${height}px`
    navSpacer.style.height = value
    document.documentElement.style.setProperty('--site-nav-dock-height', value)
  }
  if (navDock && navSpacer) {
    syncNavDockHeight()
    window.addEventListener('resize', syncNavDockHeight, { passive: true })
    window.addEventListener('load', syncNavDockHeight, { once: true })
    navDock.addEventListener('load', syncNavDockHeight, true)
    if ('ResizeObserver' in window) new ResizeObserver(syncNavDockHeight).observe(navDock)
  }

  const closeSearch = () => {
    if (!searchSheet) return
    searchSheet.hidden = true
    document.documentElement.classList.remove('has-dialog')
  }
  const openSearch = () => {
    if (!searchSheet) return
    if (mobileMenu) mobileMenu.open = false
    searchSheet.hidden = false
    document.documentElement.classList.add('has-dialog')
    window.setTimeout(() => searchInput?.focus(), 0)
  }

  document.querySelectorAll('[data-search-open]').forEach((button) => button.addEventListener('click', openSearch))
  document.querySelectorAll('[data-search-close]').forEach((button) => button.addEventListener('click', closeSearch))
  mobileMenu?.addEventListener('toggle', () => {
    if (!mobileMenu.open) return
    closeSearch()
  })
  mobileMenu?.querySelectorAll('a').forEach((link) => link.addEventListener('click', () => { mobileMenu.open = false }))

  document.addEventListener('keydown', (event) => {
    const target = event.target
    const typing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target?.isContentEditable
    if (event.key === '/' && !typing) {
      event.preventDefault()
      openSearch()
    }
    if (event.key === 'Escape') {
      closeSearch()
      if (mobileMenu) mobileMenu.open = false
    }
  })

  document.querySelectorAll('[data-episode-box]').forEach((box) => {
    const panels = [...box.querySelectorAll('[data-season]')]
    box.querySelectorAll('[data-season-tab]').forEach((tab) => {
      tab.addEventListener('click', () => {
        const season = tab.dataset.seasonTab
        panels.forEach((panel) => { panel.hidden = panel.dataset.season !== season })
        box.querySelectorAll('[data-season-tab]').forEach((button) => {
          const active = button === tab
          button.classList.toggle('is-current', active)
          button.setAttribute('aria-selected', active ? 'true' : 'false')
        })
      })
    })
  })

  document.querySelectorAll('[data-reaction-url]').forEach((row) => {
    row.querySelectorAll('[data-vote]').forEach((button) => {
      button.addEventListener('click', async () => {
        try {
          const result = await requestJson(row.dataset.reactionUrl, { method: 'POST', body: JSON.stringify({ value: Number(button.dataset.vote) }) })
          const likes = row.querySelector('[data-like-count]')
          const dislikes = row.querySelector('[data-dislike-count]')
          if (likes) likes.textContent = result.likes
          if (dislikes) dislikes.textContent = result.dislikes
          row.querySelectorAll('[data-vote]').forEach((item) => item.classList.toggle('is-active', Number(item.dataset.vote) === result.value))
        } catch (error) { window.alert(error.message) }
      })
    })
  })

  const sendReport = async (button) => {
    const reason = window.prompt('Why does this subtitle need attention?')
    if (!reason?.trim()) return
    try {
      await requestJson(button.dataset.reportUrl, { method: 'POST', body: JSON.stringify({ reason: reason.trim() }) })
      button.textContent = 'Reported'
      button.disabled = true
    } catch (error) { window.alert(error.message) }
  }

  document.addEventListener('click', (event) => {
    const report = event.target.closest('[data-report-url]')
    if (report) {
      event.preventDefault()
      sendReport(report)
      return
    }
    const like = event.target.closest('[data-comment-like-url]')
    if (!like) return
    event.preventDefault()
    requestJson(like.dataset.commentLikeUrl, { method: 'POST', body: '{}' })
      .then((result) => {
        const count = like.querySelector('span')
        if (count) count.textContent = result.likes
      })
      .catch((error) => window.alert(error.message))
  })

  document.querySelectorAll('[data-comment-form]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault()
      const status = form.querySelector('[data-comment-status]')
      const fields = new FormData(form)
      status.textContent = 'Posting…'
      try {
        const result = await requestJson(form.dataset.postUrl, { method: 'POST', body: JSON.stringify({ name: fields.get('name'), text: fields.get('text') }) })
        const list = form.closest('[data-comments]')?.querySelector('[data-comment-list]')
        if (list) {
          list.querySelector('.discussion-empty')?.remove()
          const comment = document.createElement('article')
          comment.className = 'discussion-comment'
          const header = document.createElement('header')
          const author = document.createElement('b')
          const date = document.createElement('span')
          const body = document.createElement('p')
          const like = document.createElement('button')
          const likes = document.createElement('span')
          author.textContent = result.item.name
          date.textContent = result.item.createdAt || 'Just now'
          body.textContent = result.item.body
          like.type = 'button'
          like.dataset.commentLikeUrl = `/api/public/comments/${result.item.id}/reaction`
          like.append('Like ', likes)
          likes.textContent = '0'
          header.append(author, date)
          comment.append(header, body, like)
          list.prepend(comment)
        }
        form.reset()
        status.textContent = 'Comment posted.'
      } catch (error) { status.textContent = error.message }
    })
  })
})()
